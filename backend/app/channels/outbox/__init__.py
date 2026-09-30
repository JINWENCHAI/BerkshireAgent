"""Persistent outbox for outbound channel messages.

When the Gateway restarts (crash, deploy, OOM) between the moment
``ChannelManager`` writes a final assistant reply into the LangGraph
checkpoint and the moment the channel adapter actually delivers it to the
IM platform, the user never sees that reply. The LangGraph checkpoint
records the conversation state but has no knowledge of whether the
outbound delivery succeeded.

This module owns a small SQLite table that durably records every
``OutboundMessage`` the dispatcher wants to publish. Each row is
``pending`` at insert time and becomes ``delivered`` after the channel
adapter's send callback returns successfully. On Gateway restart the
``replay`` step scans for ``pending`` rows older than a safety horizon
and re-dispatches them to the live channel, so the user eventually
receives the orphaned reply.

Design notes (kept terse, the long form lives in ``AGENTS.md``):

- The outbox is **independent of the LangGraph checkpointer database** —
  they are deployed, backed up, and migrated on different cadences. A
  corruption in one must not take the other down.
- The outbox is **per-channel** in the dispatch step but the table is
  shared across channels. The replay step filters by ``channel_name``
  so a misbehaving channel cannot drag a healthy one into a retry storm.
- Delivery is **at-least-once**. A platform that ack'd our HTTP send
  may still have lost the message in transit; we keep the row until
  ``delivered_at`` is set by the channel's success callback. Idempotency
  on the platform side (e.g. QQ's ``msg_id``) is the receiver's job.
- Streaming chunks (``is_final=False``) are intentionally **not**
  persisted — only the final reply survives a restart. Mid-stream
  patches on QQ (which has no in-place edit for bot-initiated replies)
  cannot be replayed meaningfully, so persisting them would just bloat
  the table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Cap on how old a pending row can be before we stop replaying it.
# 24h covers normal deploy/crash loops; anything older is either noise
# or a stuck row that a manual operator should clear.
DEFAULT_PENDING_HORIZON_SECONDS = 24 * 60 * 60

# Soft cap on rows retained in the table. The replay step trims oldest
# delivered rows beyond this so the file does not grow without bound
# even under load.
DEFAULT_MAX_RETAINED_ROWS = 100_000


@dataclass(slots=True)
class OutboundRecord:
    """Materialised outbox row, ready to be handed to a channel.

    The ``created_at`` epoch mirrors ``OutboundMessage.created_at`` when
    present so a replayed message keeps the original timestamp and the
    adapter can render ``[delayed]`` markers if it chooses to.
    """

    id: int
    channel_name: str
    chat_id: str
    thread_id: str
    text: str
    is_final: bool
    thread_ts: str | None
    connection_id: str | None
    owner_user_id: str | None
    artifacts_json: str
    attachments_json: str
    metadata_json: str
    created_at: float
    enqueued_at: float

    def to_outbound_message(self) -> Any:
        """Rehydrate into the canonical ``OutboundMessage`` shape."""
        # Local import to avoid a cycle with the message_bus module.
        import json as _json

        from app.channels.message_bus import OutboundMessage, ResolvedAttachment

        artifacts = list(_json.loads(self.artifacts_json) if self.artifacts_json else [])
        attachments_data = _json.loads(self.attachments_json) if self.attachments_json else []
        attachments = [ResolvedAttachment(**item) for item in attachments_data]
        metadata = _json.loads(self.metadata_json) if self.metadata_json else {}

        return OutboundMessage(
            channel_name=self.channel_name,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
            text=self.text,
            artifacts=artifacts,
            attachments=attachments,
            is_final=self.is_final,
            thread_ts=self.thread_ts,
            connection_id=self.connection_id,
            owner_user_id=self.owner_user_id,
            metadata=metadata,
            created_at=self.created_at or self.enqueued_at or time.time(),
        )


def build_message_from_row(row: Any) -> Any:
    """Adapter helper for callers that have an SQLAlchemy row."""
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
        enqueued_at=float(row.enqueued_at_epoch or 0.0),
    ).to_outbound_message()
