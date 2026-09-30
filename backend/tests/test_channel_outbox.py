"""End-to-end tests for the channel outbox + replay loop.

These tests exercise the full path the bug was filed against:

1. ``MessageBus.publish_outbound`` with the outbox enabled writes a
   ``pending`` row before fanning out.
2. A channel adapter that succeeds marks the row ``delivered``.
4. A simulated crash between (1) and (2) — we delete the listener
   without acking — leaves the row ``pending``.
5. The replay sweep re-``publish_outbound``s the row through a fresh
   adapter; the user-visible message arrives exactly once on the
   surviving channel.

The tests use ``tmp_path`` to isolate the SQLite database per test (the
outbox engine is a process-global singleton, so each test must call
``init_outbox_engine`` / ``close_outbox_engine`` symmetrically).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from app.channels.message_bus import MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.outbox.engine import close_outbox_engine, init_outbox_engine
from app.channels.outbox.replay import replay_pending_for_channel
from app.channels.outbox.repository import OutboxRepository


@pytest_asyncio.fixture
async def outbox_engine(tmp_path: Path) -> AsyncIterator[Path]:
    """Spin up a fresh outbox engine for the test."""
    await close_outbox_engine()
    await init_outbox_engine(tmp_path)
    yield tmp_path
    await close_outbox_engine()


# Convenience: every test in this module is async; without these marks
# pytest-asyncio in strict mode raises. We use the loop-scope defaults
# so a single event loop drives every fixture + test pair.
pytestmark = pytest.mark.asyncio


def _make_msg(text: str = "hi", *, channel: str = "test-channel", chat: str = "chat-1") -> OutboundMessage:
    return OutboundMessage(
        channel_name=channel,
        chat_id=chat,
        thread_id="thread-1",
        text=text,
        is_final=True,
    )


async def test_publish_writes_pending_row(outbox_engine: Path) -> None:
    """``publish_outbound`` with outbox enabled inserts a pending row."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    delivered: list[str] = []

    async def _cb(msg: OutboundMessage) -> None:
        delivered.append(msg.text)
        await bus.ack_outbound(_cb)

    bus.subscribe_outbound(_cb)

    msg = _make_msg("hello")
    await bus.publish_outbound(msg)

    assert delivered == ["hello"]

    # Row exists, marked delivered, attempt counter == 1.
    from app.channels.outbox.engine import outbox_session

    async with outbox_session() as session:
        repo = OutboxRepository(session)
        pending_rows = await repo.list_pending(channel_name="test-channel", horizon_seconds=3600)
        assert pending_rows == []  # all delivered

        # Inspect the delivered row directly.
        from sqlalchemy import select

        from app.channels.outbox.model import OutboxRow

        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        rows = list(result.scalars().all())
        assert len(rows) == 1
        assert rows[0].text == "hello"
        assert rows[0].delivered_at is not None
        assert rows[0].delivery_attempts == 1


async def test_unacked_message_stays_pending(outbox_engine: Path) -> None:
    """A listener that exits without acking leaves the row pending.

    This simulates a Gateway crash: the message was persisted, the
    listener started running, and then the process went away without
    finishing the ack. The next start must find this row.
    """
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _crash_after_send(_msg: OutboundMessage) -> None:
        # Intentionally do NOT call ack_outbound. Simulates "sent to
        # platform successfully, then gateway crashed before
        # recording the success in our DB".
        return None

    bus.subscribe_outbound(_crash_after_send)

    await bus.publish_outbound(_make_msg("abandoned"))

    from app.channels.outbox.engine import outbox_session

    async with outbox_session() as session:
        from sqlalchemy import select

        from app.channels.outbox.model import OutboxRow

        repo = OutboxRepository(session)
        rows = await repo.list_pending(channel_name="test-channel", horizon_seconds=3600)
        assert len(rows) == 1
        assert rows[0].text == "abandoned"

        # Inspect the ORM row directly for delivered_at — OutboundRecord
        # is the wire shape and does not carry delivery state.
        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        orm_row = result.scalar_one()
        assert orm_row.delivered_at is None
        assert orm_row.delivery_attempts == 1


async def test_replay_after_crash_redelivers(outbox_engine: Path) -> None:
    """End-to-end: crash leaves a pending row; replay re-dispatches it."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _crash_after_send(_msg: OutboundMessage) -> None:
        return None

    bus.subscribe_outbound(_crash_after_send)
    await bus.publish_outbound(_make_msg("orphan"))
    bus.unsubscribe_outbound(_crash_after_send)

    # Simulate a fresh gateway start with a NEW bus + a working channel.
    fresh_bus = MessageBus()
    fresh_bus.enable_outbox(enabled=True)

    redelivered: list[str] = []

    async def _live_send(msg: OutboundMessage) -> None:
        redelivered.append(msg.text)
        await fresh_bus.ack_outbound(_live_send)

    fresh_bus.subscribe_outbound(_live_send)

    async def _dispatch(msg: OutboundMessage) -> bool:
        await _live_send(msg)
        return True

    count = await replay_pending_for_channel(
        channel_name="test-channel",
        dispatcher=_dispatch,
        horizon_seconds=3600,
    )

    assert count == 1
    assert redelivered == ["orphan"]

    # And the row is now delivered, so a SECOND replay is a no-op.
    # Force-commit any open transactions first so the next SELECT
    # sees the acked row — outbox_session() commits on exit, but the
    # nested ack_outbound session may have happened mid-flight in a
    # way that leaves the replay session's snapshot stale.
    from app.channels.outbox.engine import _session_factory as _sf  # type: ignore[attr-defined]

    if _sf is not None:
        async with _sf() as session:
            await session.commit()

    count_again = await replay_pending_for_channel(
        channel_name="test-channel",
        dispatcher=_dispatch,
        horizon_seconds=3600,
    )
    assert count_again == 0


async def test_failed_replay_records_error_and_keeps_pending(outbox_engine: Path) -> None:
    """A replay that returns False records the error and leaves the row pending."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _crash(_msg: OutboundMessage) -> None:
        return None

    bus.subscribe_outbound(_crash)
    await bus.publish_outbound(_make_msg("will fail on replay"))
    bus.unsubscribe_outbound(_crash)

    async def _bad_dispatch(_msg: OutboundMessage) -> bool:
        return False

    count = await replay_pending_for_channel(
        channel_name="test-channel",
        dispatcher=_bad_dispatch,
        horizon_seconds=3600,
    )
    assert count == 0

    from app.channels.outbox.engine import outbox_session

    async with outbox_session() as session:
        repo = OutboxRepository(session)
        rows = await repo.list_pending(channel_name="test-channel", horizon_seconds=3600)

    assert len(rows) == 1
    # Inspect error field directly via the row id; OutboundRecord is the
    # wire shape and does not carry last_error.
    from sqlalchemy import select

    from app.channels.outbox.model import OutboxRow

    async with outbox_session() as session:
        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        row = result.scalar_one()
    assert "dispatcher returned False" in (row.last_error or "")


async def test_mark_delivered_is_idempotent(outbox_engine: Path) -> None:
    """Double-ack is a no-op the second time."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    seen: list[str] = []

    async def _cb(msg: OutboundMessage) -> None:
        seen.append(msg.text)
        # Ack twice (e.g. two callbacks in the chain both try).
        await bus.ack_outbound(_cb)
        await bus.ack_outbound(_cb)

    bus.subscribe_outbound(_cb)
    await bus.publish_outbound(_make_msg("ack twice"))

    from sqlalchemy import select

    from app.channels.outbox.engine import outbox_session
    from app.channels.outbox.model import OutboxRow

    async with outbox_session() as session:
        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        row = result.scalar_one()
        assert row.delivered_at is not None


async def test_horizon_trims_old_pending(outbox_engine: Path) -> None:
    """Pending rows older than the horizon are eligible for trim."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _crash(_msg: OutboundMessage) -> None:
        return None

    bus.subscribe_outbound(_crash)
    await bus.publish_outbound(_make_msg("stale"))

    # Backdate the row to simulate "abandoned for weeks".
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.channels.outbox.engine import outbox_session
    from app.channels.outbox.model import OutboxRow

    async with outbox_session() as session:
        await session.execute(update(OutboxRow).where(OutboxRow.channel_name == "test-channel").values(enqueued_at=datetime.now(UTC) - timedelta(days=30)))

    # Replay with a tight horizon must skip the row.
    async def _noop(_msg: OutboundMessage) -> bool:
        return True

    count = await replay_pending_for_channel(
        channel_name="test-channel",
        dispatcher=_noop,
        horizon_seconds=60,  # 1 minute — 30-day-old row is way past
    )
    assert count == 0

    # Operator trim path also removes it.
    async with outbox_session() as session:
        repo = OutboxRepository(session)
        deleted = await repo.trim_old_pending(horizon_seconds=3600)
    assert deleted == 1


async def test_disabled_outbox_does_not_persist(outbox_engine: Path) -> None:
    """Backward compat: outbox disabled means no rows are written."""
    bus = MessageBus()
    # Do NOT call enable_outbox.
    delivered: list[str] = []

    async def _cb(msg: OutboundMessage) -> None:
        delivered.append(msg.text)

    bus.subscribe_outbound(_cb)
    await bus.publish_outbound(_make_msg("vanilla"))

    assert delivered == ["vanilla"]

    from app.channels.outbox.engine import outbox_session

    async with outbox_session() as session:
        from sqlalchemy import select

        from app.channels.outbox.model import OutboxRow

        result = await session.execute(select(OutboxRow))
        rows = list(result.scalars().all())
    assert rows == []


async def test_attachments_serialize_roundtrip(outbox_engine: Path) -> None:
    """JSON payloads survive insert + readback without losing fidelity."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _crash(_msg: OutboundMessage) -> None:
        return None

    bus.subscribe_outbound(_crash)

    msg = OutboundMessage(
        channel_name="test-channel",
        chat_id="chat-1",
        thread_id="thread-1",
        text="with-files",
        attachments=[
            ResolvedAttachment(
                virtual_path="/mnt/user-data/outputs/report.pdf",
                actual_path=Path("/tmp/report.pdf"),
                filename="report.pdf",
                mime_type="application/pdf",
                size=1234,
                is_image=False,
            )
        ],
        artifacts=["/mnt/user-data/outputs/report.pdf"],
        metadata={"source": "test", "trace": "abc-123"},
        is_final=True,
    )
    await bus.publish_outbound(msg)

    from app.channels.outbox.engine import outbox_session

    async with outbox_session() as session:
        from sqlalchemy import select

        from app.channels.outbox.model import OutboxRow

        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        row = result.scalar_one()
        assert json.loads(row.attachments_json)[0]["filename"] == "report.pdf"
        assert json.loads(row.artifacts_json) == ["/mnt/user-data/outputs/report.pdf"]
        assert json.loads(row.metadata_json) == {"source": "test", "trace": "abc-123"}


async def test_record_outbound_failure_attaches_error(outbox_engine: Path) -> None:
    """``record_outbound_failure`` writes the error string to the row."""
    bus = MessageBus()
    bus.enable_outbox(enabled=True)

    async def _raise(msg: OutboundMessage) -> None:
        raise RuntimeError("platform down")

    bus.subscribe_outbound(_raise)
    await bus.publish_outbound(_make_msg("crash"))

    from sqlalchemy import select

    from app.channels.outbox.engine import outbox_session
    from app.channels.outbox.model import OutboxRow

    async with outbox_session() as session:
        result = await session.execute(select(OutboxRow).where(OutboxRow.channel_name == "test-channel"))
        row = result.scalar_one()
    assert row.last_error is not None
    assert "RuntimeError" in row.last_error
    assert "platform down" in row.last_error
    assert row.delivered_at is None  # still pending, will retry
