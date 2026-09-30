"""Replay loop: drain pending rows on startup and on a periodic timer.

There are two entry points:

- ``replay_pending_for_channel(...)`` is called once per channel by
  ``ChannelService.start_channel`` after the channel's ``start()`` has
  finished wiring the bus callback. We deliberately re-dispatch into
  the channel's own ``_on_outbound`` (which already knows how to ack)
  rather than calling ``channel.send()`` directly, so the same code
  path that handles live messages also handles replays. No divergence.
- ``start_periodic_replay(...)`` installs a background task that sweeps
  every ``replay_interval_seconds``. This catches the case where a row
  is enqueued, the adapter throws once (so the row stays pending), and
  the channel does not get a follow-up message to retrigger a sweep.

Crash-safety of the replay itself: a crash mid-replay leaves the row
in ``pending`` (we never delete pending rows), so the next Gateway
boot will pick it up again. The unique ``(channel, channel_message_id)``
index guarantees a platform-side ``msg_id`` only succeeds once.

Replay ack contract — the dispatcher MUST call
``bus.ack_outbound(<callback registered with bus>)`` exactly once per
invocation, just like a live publish would. Without the ack the row
stays ``pending`` and the next sweep retries — correct at-least-once
behaviour, but every retry spams the platform. The dispatcher closure
usually reuses the live ``Channel._on_outbound`` which already does
this; see ``ChannelService._replay_pending_for`` for the wiring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.channels.outbox import DEFAULT_PENDING_HORIZON_SECONDS
from app.channels.outbox.engine import outbox_session
from app.channels.outbox.repository import OutboxRepository

logger = logging.getLogger(__name__)


# A dispatcher takes the rehydrated ``OutboundMessage`` and pushes it
# through whatever the channel uses to send. Returning ``True`` means
# the message was accepted for delivery (the adapter's own success
# callback will ack the row); ``False`` means it failed synchronously,
# and the row stays pending for the next sweep.
OutboundDispatcher = Callable[[Any], Awaitable[bool]]


async def replay_pending_for_channel(
    *,
    channel_name: str,
    dispatcher: OutboundDispatcher,
    horizon_seconds: int = DEFAULT_PENDING_HORIZON_SECONDS,
    batch_size: int = 100,
) -> int:
    """Replay one channel's pending rows through ``dispatcher``.

    Returns the number of rows successfully re-dispatched. Errors are
    recorded against the row via ``record_failure`` and the row stays
    pending; we never abort the loop on a single-row failure.

    Why we ack from here instead of relying on ``bus.ack_outbound``:
    the live publish path stashes the row id in
    ``MessageBus._pending_outbox_ids[callback]`` before fanning out,
    so the channel's ``_on_outbound`` can ``ack_outbound(callback)``
    and we know exactly which row to mark delivered. Replay dispatches
    a row through ``dispatcher`` that is *not* a registered bus
    listener, so the ``_pending_outbox_ids`` lookup would miss. We
    therefore ack directly here, keyed by the row id we read.
    """
    dispatched = 0
    while True:
        async with outbox_session() as session:
            repo = OutboxRepository(session)
            pending = await repo.list_pending(
                channel_name=channel_name,
                horizon_seconds=horizon_seconds,
                limit=batch_size,
            )
            if not pending:
                return dispatched

            # We re-dispatch inside the same transaction window so a
            # crash mid-loop leaves the rows claimable next time.
            for record in pending:
                try:
                    msg = record.to_outbound_message()
                    ok = await dispatcher(msg)
                    if ok:
                        await repo.mark_delivered(row_id=record.id)
                        dispatched += 1
                    else:
                        await repo.record_failure(
                            row_id=record.id,
                            error="dispatcher returned False",
                        )
                except Exception as exc:  # noqa: BLE001 — keep the sweep alive
                    logger.exception(
                        "[Outbox] replay failed for row_id=%s channel=%s",
                        record.id,
                        channel_name,
                    )
                    await repo.record_failure(
                        row_id=record.id,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        # Loop again in case more rows accumulated during the sweep
        # (e.g. another channel mid-replay enqueued fresh pending).
        if len(pending) < batch_size:
            return dispatched


async def start_periodic_replay(
    *,
    channel_names: list[str],
    dispatcher_factory: Callable[[str], Awaitable[OutboundDispatcher] | OutboundDispatcher],
    interval_seconds: float = 30.0,
    horizon_seconds: int = DEFAULT_PENDING_HORIZON_SECONDS,
) -> asyncio.Task[int]:
    """Launch a background sweep that replays any rows the live path missed.

    Returns the asyncio.Task so the caller can keep a reference and
    cancel it on shutdown. ``dispatcher_factory`` is called once per
    channel name so the dispatcher can be bound to the live channel
    instance — keeping replay and live dispatch in lockstep.

    The task exits cleanly when its outer coroutine returns (we never
    cancel from inside the loop); ``ChannelService`` is expected to
    cancel the task during ``stop()``.
    """

    async def _run() -> int:
        # Stagger the per-channel sweeps so a fleet of channels does not
        # all hammer the DB at t=0 on a cold start.
        for offset, name in enumerate(channel_names):
            await asyncio.sleep(offset * 0.1)

        while True:
            for name in channel_names:
                try:
                    dispatcher = await _maybe_await(dispatcher_factory(name))
                    await replay_pending_for_channel(
                        channel_name=name,
                        dispatcher=dispatcher,
                        horizon_seconds=horizon_seconds,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[Outbox] periodic replay sweep failed for channel=%s", name)
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_run(), name="channel-outbox-replay")


async def _maybe_await(value: Any) -> Any:
    """Await if ``value`` is awaitable; pass through otherwise.

    Lets ``dispatcher_factory`` return either a ready dispatcher or a
    coroutine that resolves to one — handy when factory needs to do
    some async lookup before binding.
    """
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value
