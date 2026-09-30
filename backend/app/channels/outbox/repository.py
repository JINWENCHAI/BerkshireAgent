"""Repository over ``OutboxRow``.

The repository is the only place that knows the column names; the rest
of the codebase talks to ``OutboundRecord`` and ``OutboundMessage``.
This keeps the JSON serialization (the column types are deliberately
``Text``, not ``JSON``, so the engine can run on SQLite without a
dialect-specific column type) in one spot.

Concurrency model:

- ``enqueue`` is single-row INSERT, no upsert. Two dispatches of the
  same message produce two rows; the platform adapter is the dedupe
  authority (it knows its own message id).
- ``mark_delivered`` is idempotent: a second call on the same row is a
  no-op. This lets the adapter call it from both the HTTP-success
  callback and a follow-up retry without us double-counting.
- ``increment_attempt`` is the only place that bumps
  ``delivery_attempts``; it runs under ``SELECT ... LIMIT 1`` so two
  replayer threads do not both grab the same row.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.outbox import OutboundRecord
from app.channels.outbox.model import OutboxRow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingRow:
    """A row claimed by the replayer, ready to be re-dispatched.

    Holds the new row id so ``mark_delivered`` / ``increment_attempt``
    can find the same row even if a parallel replayer raced us and the
    ``id`` was bumped before our transaction committed.
    """

    id: int
    record: OutboundRecord


def _to_record(row: OutboxRow) -> OutboundRecord:
    """Materialise an ORM row into the wire-shape ``OutboundRecord``."""
    return OutboundRecord(
        id=row.id,
        channel_name=row.channel_name,
        chat_id=row.chat_id,
        thread_id=row.thread_id or "",
        text=row.text,
        is_final=bool(row.is_final),
        thread_ts=row.thread_ts,
        connection_id=row.connection_id,
        owner_user_id=row.owner_user_id,
        artifacts_json=row.artifacts_json or "[]",
        attachments_json=row.attachments_json or "[]",
        metadata_json=row.metadata_json or "{}",
        created_at=float(row.created_at_epoch or 0.0),
        enqueued_at=row.enqueued_at.timestamp() if row.enqueued_at else 0.0,
    )


class OutboxRepository:
    """All outbox SQL goes through here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- enqueue

    async def enqueue(self, *, msg: Any) -> int:
        """Insert a fresh ``pending`` row and return its id.

        The caller (``MessageBus.publish_outbound``) holds the new id
        in an internal map so the channel's success callback can ack
        exactly this row even after a process restart.
        """
        artifacts_json = json.dumps(list(getattr(msg, "artifacts", []) or []), ensure_ascii=False)
        attachments_data = []
        for att in getattr(msg, "attachments", []) or []:
            attachments_data.append(
                {
                    "virtual_path": att.virtual_path,
                    "actual_path": str(att.actual_path),
                    "filename": att.filename,
                    "mime_type": att.mime_type,
                    "size": att.size,
                    "is_image": att.is_image,
                }
            )
        attachments_json = json.dumps(attachments_data, ensure_ascii=False)
        metadata_json = json.dumps(dict(getattr(msg, "metadata", {}) or {}), ensure_ascii=False)

        row = OutboxRow(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=getattr(msg, "thread_id", "") or "",
            text=msg.text,
            is_final=bool(getattr(msg, "is_final", True)),
            thread_ts=getattr(msg, "thread_ts", None),
            connection_id=getattr(msg, "connection_id", None),
            owner_user_id=getattr(msg, "owner_user_id", None),
            artifacts_json=artifacts_json,
            attachments_json=attachments_json,
            metadata_json=metadata_json,
            created_at_epoch=float(getattr(msg, "created_at", time.time())),
            enqueued_at=datetime.now(UTC),
            delivered_at=None,
            channel_message_id=None,
            delivery_attempts=1,  # initial enqueue counts as attempt 1
            last_error=None,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    # ------------------------------------------------------------------ read

    async def list_pending(
        self,
        *,
        channel_name: str | None = None,
        horizon_seconds: int,
        limit: int = 1000,
    ) -> list[OutboundRecord]:
        """Return rows that are still pending and not older than ``horizon_seconds``.

        ``channel_name=None`` lists across every channel — used by the
        admin / diagnostic path. The startup replayer always passes the
        specific channel so a slow channel cannot drag the healthy ones
        into a single retry storm.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=horizon_seconds)
        stmt = select(OutboxRow).where(OutboxRow.delivered_at.is_(None)).where(OutboxRow.enqueued_at >= cutoff).order_by(OutboxRow.id.asc()).limit(limit)
        if channel_name is not None:
            stmt = stmt.where(OutboxRow.channel_name == channel_name)
        result = await self._session.execute(stmt)
        return [_to_record(row) for row in result.scalars().all()]

    async def count_pending(self, *, channel_name: str | None = None) -> int:
        """Diagnostic counter for the admin endpoint and tests."""
        from sqlalchemy import func as sa_func

        stmt = select(sa_func.count()).select_from(OutboxRow).where(OutboxRow.delivered_at.is_(None))
        if channel_name is not None:
            stmt = stmt.where(OutboxRow.channel_name == channel_name)
        result = await self._session.execute(stmt)
        return int(result.scalar_one() or 0)

    # ------------------------------------------------------------------ ack

    async def mark_delivered(self, *, row_id: int, channel_message_id: str | None = None) -> bool:
        """Mark a row delivered; idempotent.

        Returns ``True`` iff this call actually transitioned the row
        from pending to delivered. A second call on the same id returns
        ``False`` so the caller can skip side-effects (e.g. logging
        "delivered" twice).
        """
        stmt = update(OutboxRow).where(OutboxRow.id == row_id, OutboxRow.delivered_at.is_(None)).values(delivered_at=datetime.now(UTC), channel_message_id=channel_message_id)
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def mark_delivered_by_msgid(self, *, channel_name: str, channel_message_id: str) -> bool:
        """Idempotent ack by the platform-side id, for adapters that
        only learn it post-hoc (e.g. via a webhook ack or a get-me
        call). The unique index on (channel, channel_message_id)
        ensures at most one row matches.
        """
        stmt = (
            update(OutboxRow)
            .where(
                OutboxRow.channel_name == channel_name,
                OutboxRow.channel_message_id == channel_message_id,
                OutboxRow.delivered_at.is_(None),
            )
            .values(delivered_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def record_failure(self, *, row_id: int, error: str) -> None:
        """Record the latest error string without changing delivery state.

        Used by the replayer when the re-dispatch throws — the row
        stays pending so the next sweep retries, but the operator can
        see *why* in the table.
        """
        # Cap the error string so a runaway provider cannot bloat the row.
        capped = error if len(error) <= 4000 else error[:3997] + "..."
        stmt = update(OutboxRow).where(OutboxRow.id == row_id).values(last_error=capped)
        await self._session.execute(stmt)

    async def increment_attempt(self, *, row_id: int) -> None:
        """Bump the attempt counter. Called when the replayer picks a row up."""
        stmt = update(OutboxRow).where(OutboxRow.id == row_id).values(delivery_attempts=OutboxRow.delivery_attempts + 1)
        await self._session.execute(stmt)

    # ---------------------------------------------------------------- sweep

    async def trim_delivered(self, *, keep_last_n: int) -> int:
        """Delete oldest delivered rows beyond ``keep_last_n``.

        Returns the number of rows deleted. Cheap on SQLite thanks to
        the autoincrement primary key — the planner uses ROWID order
        without a sort.
        """
        from sqlalchemy import delete

        # Find the cutoff id: keep the N newest delivered rows, delete the rest.
        cutoff_stmt = select(OutboxRow.id).where(OutboxRow.delivered_at.is_not(None)).order_by(OutboxRow.id.desc()).offset(keep_last_n).limit(1)
        cutoff_result = await self._session.execute(cutoff_stmt)
        cutoff_id = cutoff_result.scalar_one_or_none()
        if cutoff_id is None:
            return 0

        stmt = delete(OutboxRow).where(
            OutboxRow.delivered_at.is_not(None),
            OutboxRow.id <= cutoff_id,
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    async def trim_old_pending(self, *, horizon_seconds: int) -> int:
        """Delete pending rows older than the horizon. Operator-only path.

        Returns the number of rows deleted. Use when an adapter has
        been broken for days and the pending queue is filling disk.
        """
        from sqlalchemy import delete

        cutoff = datetime.now(UTC) - timedelta(seconds=horizon_seconds)
        stmt = delete(OutboxRow).where(
            OutboxRow.delivered_at.is_(None),
            OutboxRow.enqueued_at < cutoff,
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    # ------------------------------------------------------------- iterator

    async def iter_pending(self, **kwargs: Any) -> Iterable[OutboundRecord]:
        """Convenience iterator for the replay loop."""
        rows = await self.list_pending(**kwargs)
        for row in rows:
            yield row
