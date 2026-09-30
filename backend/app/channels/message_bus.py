"""MessageBus — async pub/sub hub that decouples channels from the agent dispatcher."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_QUEUE_MAXSIZE = 1000

PENDING_CLARIFICATION_METADATA_KEY = "pending_clarification"
RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY = "resolved_from_pending_clarification"
# Adapter-owned bytes may use this transient key while crossing the channel
# boundary. ChannelManager consumes and removes it before persisting metadata.
INBOUND_FILE_CONTENT_KEY = "_content"


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class InboundMessageType(StrEnum):
    """Types of messages arriving from IM channels."""

    CHAT = "chat"
    COMMAND = "command"


@dataclass
class InboundMessage:
    """A message arriving from an IM channel toward the agent dispatcher.

    Attributes:
        channel_name: Name of the source channel (e.g. "feishu", "slack").
        chat_id: Platform-specific chat/conversation identifier.
        user_id: Platform-specific user identifier.
        text: The message text.
        msg_type: Whether this is a regular chat message or a command.
        thread_ts: Optional platform thread identifier (for threaded replies).
        topic_id: Conversation topic identifier used to map to a BerkshireAgent thread.
            Messages sharing the same ``topic_id`` within a ``chat_id`` will
            reuse the same BerkshireAgent thread.  When ``None``, each message
            creates a new thread (one-shot Q&A).
        connection_id: Optional BerkshireAgent channel connection id. When present,
            conversation mapping is scoped by the connection instead of the
            legacy global ``channel_name:chat_id[:topic_id]`` key.
        owner_user_id: BerkshireAgent user id that owns the channel connection.
            Platform user ids stay in ``user_id``.
        workspace_id: Optional external workspace/guild/team id.
        files: Optional list of file attachments (platform-specific dicts).
        metadata: Arbitrary extra data from the channel.
        created_at: Unix timestamp when the message was created.
    """

    channel_name: str
    chat_id: str
    user_id: str
    text: str
    msg_type: InboundMessageType = InboundMessageType.CHAT
    thread_ts: str | None = None
    topic_id: str | None = None
    connection_id: str | None = None
    owner_user_id: str | None = None
    workspace_id: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class ResolvedAttachment:
    """A file attachment resolved to a host filesystem path, ready for upload.

    Attributes:
        virtual_path: Original virtual path (e.g. /mnt/user-data/outputs/report.pdf).
        actual_path: Resolved host filesystem path.
        filename: Basename of the file.
        mime_type: MIME type (e.g. "application/pdf").
        size: File size in bytes.
        is_image: True for image/* MIME types (platforms may handle images differently).
    """

    virtual_path: str
    actual_path: Path
    filename: str
    mime_type: str
    size: int
    is_image: bool


@dataclass
class OutboundMessage:
    """A message from the agent dispatcher back to a channel.

    Attributes:
        channel_name: Target channel name (used for routing).
        chat_id: Target chat/conversation identifier.
        thread_id: BerkshireAgent thread ID that produced this response.
        text: The response text.
        artifacts: List of artifact paths produced by the agent.
        is_final: Whether this is the final message in the response stream.
        thread_ts: Optional platform thread identifier for threaded replies.
        metadata: Arbitrary extra data.
        connection_id: Optional BerkshireAgent channel connection id used for
            connection-specific outbound credentials.
        owner_user_id: BerkshireAgent user id that owns the channel connection.
        created_at: Unix timestamp.
    """

    channel_name: str
    chat_id: str
    thread_id: str
    text: str
    artifacts: list[str] = field(default_factory=list)
    attachments: list[ResolvedAttachment] = field(default_factory=list)
    is_final: bool = True
    thread_ts: str | None = None
    connection_id: str | None = None
    owner_user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------

OutboundCallback = Callable[[OutboundMessage], Coroutine[Any, Any, None]]


class InboundQueueFullError(RuntimeError):
    """Raised when bounded inbound admission has no capacity."""


class InboundQueueClosedError(RuntimeError):
    """Raised when inbound admission has closed during shutdown."""


class InboundReservationExpiredError(RuntimeError):
    """Raised when a reservation was already committed or invalidated."""


class InboundReservation:
    """One capacity slot reserved before a provider hands work to the bus.

    Some provider SDKs invoke BerkshireAgent on a foreign thread. Reserving before
    scheduling their final identity/ack preparation onto the Gateway loop
    bounds both the queue and those scheduled callbacks. A reservation must be
    committed exactly once or released in a ``finally`` block.
    """

    def __init__(self, bus: MessageBus, token: object) -> None:
        self._bus = bus
        self._token = token

    def commit(self, msg: InboundMessage) -> None:
        """Commit the reserved message from the MessageBus event loop."""
        self._bus._commit_inbound(self._token, msg)

    def release(self) -> None:
        """Release the slot if it has not already been committed or closed."""
        self._bus._release_inbound_reservation(self._token)


class MessageBus:
    """Async pub/sub hub connecting channels and the agent dispatcher.

    Channels publish inbound messages; the dispatcher consumes them.
    The dispatcher publishes outbound messages; channels receive them
    via registered callbacks.
    """

    def __init__(self, *, inbound_queue_maxsize: int = DEFAULT_INBOUND_QUEUE_MAXSIZE) -> None:
        if isinstance(inbound_queue_maxsize, bool) or not isinstance(inbound_queue_maxsize, int) or inbound_queue_maxsize <= 0:
            raise ValueError("inbound_queue_maxsize must be a positive integer")

        self._inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbound_queue_maxsize)
        # Provider callbacks may reserve capacity from SDK-owned threads before
        # scheduling async identity/ack preparation on the Gateway loop.
        self._inbound_admission_lock = threading.Lock()
        self._inbound_queued = 0
        self._inbound_reservations: set[object] = set()
        self._accepting_inbound = True
        self._full_rejection_count = 0
        self._last_full_warning_at = 0.0
        self._outbound_listeners: list[OutboundCallback] = []
        # Outbox integration. Disabled by default so the existing
        # in-memory fan-out behaviour is preserved; ``enable_outbox``
        # flips both flags atomically (callers are expected to call it
        # during startup, before the first publish).
        self._outbox_enabled = False
        # Map: callback -> row_id of the most recent outbox row the
        # callback is about to (or has just) processed. Consumed by
        # ``ack_outbound`` and ``record_outbound_failure``. The map is
        # intentionally unbounded only by listener count (small);
        # rows are removed on ack/failure so the size stays in check.
        self._pending_outbox_ids: dict[OutboundCallback, int] = {}

    # -- inbound -----------------------------------------------------------

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Admit a message immediately or raise instead of waiting for space.

        This deliberately uses reservation + ``put_nowait`` rather than an
        awaited ``Queue.put``. Under overload, producers get an explicit
        rejection and cannot accumulate an unbounded set of pending put tasks.
        The initial zero-delay sleep is a scheduling handoff, not a capacity
        wait: a producer that publishes a batch (notably GitHub webhook
        fan-out) gives fixed workers a chance to dequeue between entries so a
        batch larger than the queue does not repeatedly fail on the same
        prefix during redelivery. It occurs before admission so cancellation
        cannot report failure after this call already committed the message.
        """
        await asyncio.sleep(0)
        reservation = self.reserve_inbound(msg)
        try:
            reservation.commit(msg)
        finally:
            reservation.release()

    def reserve_inbound(self, msg: InboundMessage) -> InboundReservation:
        """Reserve one bounded intake slot, safely callable from SDK threads."""
        token = object()
        should_warn = False
        rejection_count = 0
        with self._inbound_admission_lock:
            if not self._accepting_inbound:
                raise InboundQueueClosedError("channel inbound intake is closed")
            admitted = self._inbound_queued + len(self._inbound_reservations)
            if admitted >= self._inbound_queue.maxsize:
                self._full_rejection_count += 1
                rejection_count = self._full_rejection_count
                now = time.monotonic()
                if now - self._last_full_warning_at >= 1.0:
                    self._last_full_warning_at = now
                    should_warn = True
            else:
                self._inbound_reservations.add(token)
                return InboundReservation(self, token)

        if should_warn:
            logger.warning(
                "[Bus] inbound capacity exhausted: channel=%s, chat_id=%s, capacity=%d, rejected_total=%d",
                msg.channel_name,
                msg.chat_id,
                self._inbound_queue.maxsize,
                rejection_count,
            )
        raise InboundQueueFullError(f"channel inbound queue is full (capacity={self._inbound_queue.maxsize})")

    def _commit_inbound(self, token: object, msg: InboundMessage) -> None:
        with self._inbound_admission_lock:
            if token not in self._inbound_reservations:
                raise InboundReservationExpiredError("inbound reservation is no longer active")
            self._inbound_reservations.remove(token)
            if not self._accepting_inbound:
                raise InboundQueueClosedError("channel inbound intake closed before reservation commit")
            try:
                self._inbound_queue.put_nowait(msg)
            except asyncio.QueueFull as exc:  # pragma: no cover - reservation accounting invariant
                raise RuntimeError("inbound reservation accounting exceeded queue capacity") from exc
            self._inbound_queued += 1

        logger.info(
            "[Bus] inbound enqueued: channel=%s, chat_id=%s, type=%s, queue_size=%d",
            msg.channel_name,
            msg.chat_id,
            msg.msg_type.value,
            self._inbound_queue.qsize(),
        )

    def _release_inbound_reservation(self, token: object) -> None:
        with self._inbound_admission_lock:
            if token in self._inbound_reservations:
                self._inbound_reservations.remove(token)

    async def get_inbound(self) -> InboundMessage:
        """Block until the next inbound message is available."""
        msg = await self._inbound_queue.get()
        with self._inbound_admission_lock:
            self._inbound_queued -= 1
        return msg

    def get_inbound_nowait(self) -> InboundMessage:
        """Return one queued message immediately and release admission capacity."""
        msg = self._inbound_queue.get_nowait()
        with self._inbound_admission_lock:
            self._inbound_queued -= 1
        return msg

    def inbound_task_done(self) -> None:
        """Mark one dequeued inbound message as fully handled."""
        self._inbound_queue.task_done()

    async def join_inbound(self) -> None:
        """Wait until every admitted queue item has completed or been dropped."""
        await self._inbound_queue.join()

    def close_inbound(self) -> int:
        """Reject new intake and invalidate uncommitted reservations.

        Returns the number of provider-side reservations invalidated. Queued
        messages remain until workers finish or ``discard_pending_inbound`` is
        called by shutdown.
        """
        with self._inbound_admission_lock:
            self._accepting_inbound = False
            invalidated = len(self._inbound_reservations)
            self._inbound_reservations.clear()
        return invalidated

    def open_inbound(self) -> None:
        """Re-open admission when a stopped manager is explicitly restarted."""
        with self._inbound_admission_lock:
            self._accepting_inbound = True

    def discard_pending_inbound(self) -> int:
        """Drop queued, not-yet-started messages during shutdown."""
        discarded = 0
        while True:
            try:
                self.get_inbound_nowait()
            except asyncio.QueueEmpty:
                break
            self._inbound_queue.task_done()
            discarded += 1
        return discarded

    @property
    def inbound_queue_maxsize(self) -> int:
        return self._inbound_queue.maxsize

    @property
    def inbound_queue(self) -> asyncio.Queue[InboundMessage]:
        """Expose the queue for read-only size/empty inspection."""
        return self._inbound_queue

    # -- outbound ----------------------------------------------------------

    def enable_outbox(self, *, enabled: bool) -> None:
        """Toggle durable outbound persistence.

        When ``enabled`` is true, every ``publish_outbound`` call writes
        a ``pending`` row to the channel outbox before fanning out, and
        the channel's outbound callback must call ``ack_outbound`` once
        its adapter confirms delivery. Failed or crashed deliveries
        stay pending until the periodic replay sweep picks them up.

        When ``enabled`` is false (default for backward compatibility),
        the bus falls back to in-memory fan-out and a crash between
        ``publish_outbound`` and the adapter means the user loses the
        message — exactly the pre-outbox behaviour.

        Must be called before any ``publish_outbound`` to take effect;
        toggling at runtime is unsupported (the listener map and the
        outbox id map are not synchronised under a writer lock).
        """
        self._outbox_enabled = bool(enabled)
        if not self._outbox_enabled:
            self._pending_outbox_ids = {}

    @property
    def outbox_enabled(self) -> bool:
        return self._outbox_enabled

    def subscribe_outbound(self, callback: OutboundCallback) -> None:
        """Register an async callback for outbound messages."""
        self._outbound_listeners.append(callback)

    def unsubscribe_outbound(self, callback: OutboundCallback) -> None:
        """Remove a previously registered outbound callback."""
        self._outbound_listeners = [cb for cb in self._outbound_listeners if cb != callback]

    async def ack_outbound(self, callback: OutboundCallback) -> None:
        """Mark the most recent outbox row this callback received as delivered.

        No-op when the outbox is disabled. Idempotent — the underlying
        ``mark_delivered`` only transitions pending → delivered once.
        """
        if not self._outbox_enabled:
            return
        row_id = self._pending_outbox_ids.pop(callback, None)
        if row_id is None:
            return
        try:
            from app.channels.outbox.engine import outbox_session
            from app.channels.outbox.repository import OutboxRepository

            async with outbox_session() as session:
                repo = OutboxRepository(session)
                await repo.mark_delivered(row_id=row_id)
        except Exception:  # noqa: BLE001 — ack must never raise to the caller
            logger.exception("[Bus] failed to ack outbox row_id=%s", row_id)

    async def record_outbound_failure(self, callback: OutboundCallback, error: str) -> None:
        """Best-effort failure recording for the most recent outbox row.

        Used when the channel's outbound callback raises; the row stays
        pending so the next replay sweep retries, but operators see the
        error string in the row. No-op when the outbox is disabled or
        there is no row to attribute.
        """
        if not self._outbox_enabled:
            return
        row_id = self._pending_outbox_ids.get(callback)
        if row_id is None:
            return
        try:
            from app.channels.outbox.engine import outbox_session
            from app.channels.outbox.repository import OutboxRepository

            async with outbox_session() as session:
                repo = OutboxRepository(session)
                await repo.record_failure(row_id=row_id, error=error)
        except Exception:  # noqa: BLE001
            logger.exception("[Bus] failed to record outbox failure row_id=%s", row_id)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Dispatch an outbound message to all registered listeners.

        When the outbox is enabled, the message is persisted as a
        ``pending`` row first; the row id is stashed in a per-listener
        map so ``ack_outbound`` / ``record_outbound_failure`` can
        attribute the outcome back to the same row even after a
        restart. Each listener is responsible for calling one of those
        helpers from its own ``finally`` block — the bus does not ack
        on the listener's behalf, because the listener is the only
        party that knows whether the platform accepted the message.
        """
        listeners = self._outbound_listeners
        logger.info(
            "[Bus] outbound dispatching: channel=%s, chat_id=%s, listeners=%d, text_len=%d outbox=%s",
            msg.channel_name,
            msg.chat_id,
            len(listeners),
            len(msg.text),
            self._outbox_enabled,
        )

        # Persist once per publish, regardless of how many listeners
        # are attached. Multi-listener setups (e.g. a test recorder
        # plus the live channel) all see the same row id and the live
        # channel's ack covers the delivery.
        row_id: int | None = None
        if self._outbox_enabled:
            try:
                from app.channels.outbox.engine import outbox_session
                from app.channels.outbox.repository import OutboxRepository

                async with outbox_session() as session:
                    repo = OutboxRepository(session)
                    row_id = await repo.enqueue(msg=msg)
            except Exception:  # noqa: BLE001 — never block live dispatch on outbox I/O
                logger.exception("[Bus] outbox enqueue failed; dispatching live only (delivery will NOT survive a crash)")
                row_id = None

        for callback in listeners:
            if row_id is not None:
                self._pending_outbox_ids[callback] = row_id
            try:
                await callback(msg)
            except Exception as exc:  # noqa: BLE001 — one bad listener must not stop the rest
                logger.exception(
                    "Error in outbound callback for channel=%s",
                    msg.channel_name,
                )
                if row_id is not None:
                    # record_outbound_failure reads the row id from the
                    # same map, so do not pop before calling it. The map
                    # entry is cleared by the listener's own success path
                    # (which acks) — on the failure path we leave it
                    # behind so the periodic replay sweep can retry.
                    await self.record_outbound_failure(callback, f"{type(exc).__name__}: {exc}")
                    self._pending_outbox_ids.pop(callback, None)
            else:
                # Successful callback path: the listener will call
                # ack_outbound from its own finally block. We do NOT
                # clear _pending_outbox_ids here because the ack is
                # what consumes it; if the listener forgets to ack,
                # the periodic replay sweep will retry the row, which
                # is the correct at-least-once behaviour.
                continue
