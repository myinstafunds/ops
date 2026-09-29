"""Customer communication log (PRD 26, 27).

Mobile numbers are stored in full because a delivery receipt may need to be
matched, but the API only ever returns the masked form (PRD 26, 30).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime by the registry
    from app.models.work import WorkItem


class Channel(StrEnum):
    CALL = "CALL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    RCS = "RCS"


class Direction(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class CommunicationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CONNECTED = "CONNECTED"
    NO_RESPONSE = "NO_RESPONSE"


class CallDisposition(StrEnum):
    CONNECTED = "CONNECTED"
    NO_ANSWER = "NO_ANSWER"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    REFUSED = "REFUSED"
    WRONG_NUMBER = "WRONG_NUMBER"
    PAYMENT_PROMISED = "PAYMENT_PROMISED"
    NOT_INTERESTED = "NOT_INTERESTED"
    RESOLVED = "RESOLVED"


class CommunicationEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """One customer communication attempt."""

    __tablename__ = "communication_events"
    __table_args__ = (
        Index("ix_communication_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_communication_events_tenant_channel_status", "tenant_id", "channel", "status"),
        Index("ix_communication_events_tenant_work_item", "tenant_id", "work_item_id"),
        Index("ix_communication_events_tenant_customer_ref", "tenant_id", "customer_reference"),
        Index("ix_communication_events_tenant_actor", "tenant_id", "actor_user_id"),
    )

    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("work_items.id", ondelete="SET NULL"), nullable=True
    )
    source_app: Mapped[str | None] = mapped_column(String(16), nullable=True)

    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, name="communication_channel", native_enum=True), nullable=False
    )
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, name="communication_direction", native_enum=True), nullable=False
    )
    status: Mapped[CommunicationStatus] = mapped_column(
        Enum(CommunicationStatus, name="communication_status", native_enum=True),
        nullable=False,
        default=CommunicationStatus.PENDING,
    )

    customer_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Stored in full, never serialised. See ``mobile_masked``.
    customer_mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposition: Mapped[CallDisposition | None] = mapped_column(
        Enum(CallDisposition, name="call_disposition", native_enum=True), nullable=True
    )

    #: Provider correlation identifiers (Release 5).
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int | None] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    work_item: Mapped[WorkItem] = relationship(lazy="joined")
