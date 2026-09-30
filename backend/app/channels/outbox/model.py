"""SQLAlchemy ORM model for the persistent outbound outbox.

The table is intentionally schema-light. The fields mirror what
``OutboundMessage`` carries across the in-process boundary, plus a
``delivered_at`` timestamp and an attempt counter. JSON columns hold
the structured payloads (``artifacts`` / ``attachments`` /
``metadata``) so a schema bump in those dataclasses does not force an
alembic migration on this side too.

The model lives under ``app.channels.outbox`` (not ``deerflow.persistence``)
because the outbox is a channel-bus concern, not a LangGraph / app-domain
concept. Keeping it close to the channels layer also makes it easier
to reason about which deploys own it: ``app/`` ships with the
gateway, ``deerflow/`` ships with the harness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class OutboxRow(Base):
    __tablename__ = "channel_outbox"

    # Autoincrement surrogate key. ``(channel_name, channel_message_id)``
    # would be more semantic but the platform does not always supply one
    # at enqueue time, and a per-channel autoincrement is enough for the
    # replay query that scans ``WHERE delivered_at IS NULL``.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Routing. channel_name + chat_id is the dedupe key against the
    # platform's own ``msg_id`` once the adapter sets it via
    # ``mark_delivered(channel_message_id=...)``.
    channel_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # LangGraph thread that produced this reply. Stored as text so a
    # replay can join back to the checkpoint store if it ever needs to.
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Body. ``text`` is the user-visible reply; the structured payloads
    # are stored as JSON so they survive schema changes.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    thread_ts: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Connection / ownership metadata. Mirrors ``OutboundMessage``.
    connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Structured payloads. Stored as JSON text (not native JSON) so the
    # table works on both SQLite and Postgres without dialect-specific
    # column types — the engine's ``JSON`` dialect would otherwise force
    # a Postgres ``JSONB`` and a SQLite ``TEXT`` that disagree on
    # ordering / duplicate-key handling.
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # Time bookkeeping. ``created_at_epoch`` mirrors the message's own
    # ``created_at`` (so a replay keeps the original clock); the
    # timezone-aware ``enqueued_at`` is when our outbox received it.
    # ``delivered_at`` is set on successful delivery ack.
    created_at_epoch: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Platform-supplied id once the adapter learns it. Optional — only
    # the channel that issues one will populate it (QQ does; Slack
    # does; in-process channels may not).
    channel_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Attempt bookkeeping. ``delivery_attempts`` increments every time
    # the row is re-dispatched (initial enqueue counts as one); the
    # last failure is recorded as text for operator triage.
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The replay query is `WHERE delivered_at IS NULL ORDER BY id`.
        # An index on (delivered_at, id) keeps the scan cheap as the
        # table grows past the first few hundred rows.
        Index("ix_channel_outbox_pending", "channel_name", "delivered_at", "id"),
        # Channel-side dedupe: a platform redelivery (we asked twice)
        # is the adapter's problem; this index just makes the
        # ``mark_delivered`` lookup a single seek.
        Index("uq_channel_outbox_msgid", "channel_name", "channel_message_id", unique=True),
    )

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """Mirror ``Base.to_dict``; called out explicitly so a future
        refactor that moves the ORM to a different base does not
        silently change the row shape consumed by the replay step.
        """
        return super().to_dict(exclude=exclude)
