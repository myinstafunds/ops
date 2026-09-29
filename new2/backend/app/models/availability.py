"""Availability windows (PRD 14).

Rules encoded in the model and enforced in ``app.services.availability``:

* self approval is impossible - the database forbids the creator approving;
* the original creator and approver are never overwritten by later edits;
* an APPROVED window covering "now" blocks new allocation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime by the registry
    from app.models.user import User


class AvailabilityType(StrEnum):
    LEAVE = "LEAVE"
    ABSENT = "ABSENT"
    BREAK = "BREAK"
    TRAINING = "TRAINING"
    OFFLINE = "OFFLINE"


class AvailabilityRequestStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


#: Types that stop new work being allocated. BREAK is working time.
BLOCKING_TYPES: frozenset[AvailabilityType] = frozenset(
    {
        AvailabilityType.LEAVE,
        AvailabilityType.ABSENT,
        AvailabilityType.TRAINING,
        AvailabilityType.OFFLINE,
    }
)

#: Only these statuses can block allocation.
BLOCKING_STATUSES: frozenset[AvailabilityRequestStatus] = frozenset(
    {AvailabilityRequestStatus.APPROVED}
)


class UserAvailability(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """A window during which a user should not receive new work."""

    __tablename__ = "user_availability"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        # Self approval is impossible in the storage layer, not only in the
        # service. Even a future code path cannot write an approved window
        # that the requester also approved (PRD 15.2).
        CheckConstraint(
            "created_by_user_id IS NULL OR decided_by_user_id IS DISTINCT FROM created_by_user_id",
            name="no_self_approval",
        ),
        Index(
            "ix_user_availability_tenant_user_window",
            "tenant_id",
            "user_id",
            "status",
            "start_at",
            "end_at",
        ),
        Index("ix_user_availability_tenant_status", "tenant_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_users.id", ondelete="CASCADE"), nullable=False
    )
    availability_type: Mapped[AvailabilityType] = mapped_column(
        Enum(AvailabilityType, name="availability_type", native_enum=True), nullable=False
    )
    status: Mapped[AvailabilityRequestStatus] = mapped_column(
        Enum(AvailabilityRequestStatus, name="availability_request_status", native_enum=True),
        nullable=False,
        default=AvailabilityRequestStatus.REQUESTED,
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- provenance: written once, never overwritten ---------------------
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- edit history ----------------------------------------------------
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # This table points at ``ops_users`` three times (the subject, the creator
    # and the approver), so the relationship must name which one it follows.
    user: Mapped[User] = relationship(lazy="joined", foreign_keys=[user_id])

    @property
    def blocks_allocation(self) -> bool:
        return self.availability_type in BLOCKING_TYPES and self.status in BLOCKING_STATUSES

    def covers(self, moment: datetime) -> bool:
        """True when this window blocks allocation at ``moment``."""
        return self.blocks_allocation and self.start_at <= moment < self.end_at


__all__ = [
    "BLOCKING_STATUSES",
    "BLOCKING_TYPES",
    "AvailabilityRequestStatus",
    "AvailabilityType",
    "UserAvailability",
]
