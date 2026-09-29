"""Work items, their event timeline and case comments (PRD 15, 18, 19, 21)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.org import SourceApp
from app.models.sla import SlaStatus


class WorkStatus(StrEnum):
    """Work item lifecycle (PRD 15)."""

    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    ON_HOLD = "ON_HOLD"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


OPEN_STATUSES: frozenset[WorkStatus] = frozenset(
    {WorkStatus.OPEN, WorkStatus.ASSIGNED, WorkStatus.IN_PROGRESS, WorkStatus.ON_HOLD}
)

TERMINAL_STATUSES: frozenset[WorkStatus] = frozenset({WorkStatus.CLOSED, WorkStatus.CANCELLED})

#: Allowed transitions. Enforced in the service layer and covered by tests.
ALLOWED_TRANSITIONS: dict[WorkStatus, frozenset[WorkStatus]] = {
    WorkStatus.OPEN: frozenset(
        {WorkStatus.ASSIGNED, WorkStatus.ON_HOLD, WorkStatus.CLOSED, WorkStatus.CANCELLED}
    ),
    WorkStatus.ASSIGNED: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.OPEN,
            WorkStatus.ON_HOLD,
            WorkStatus.CLOSED,
            WorkStatus.CANCELLED,
        }
    ),
    WorkStatus.IN_PROGRESS: frozenset(
        {WorkStatus.ASSIGNED, WorkStatus.ON_HOLD, WorkStatus.CLOSED, WorkStatus.CANCELLED}
    ),
    WorkStatus.ON_HOLD: frozenset(
        {
            WorkStatus.ASSIGNED,
            WorkStatus.IN_PROGRESS,
            WorkStatus.OPEN,
            WorkStatus.CLOSED,
            WorkStatus.CANCELLED,
        }
    ),
    WorkStatus.CLOSED: frozenset(),
    WorkStatus.CANCELLED: frozenset(),
}


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


PRIORITY_WEIGHT: dict[str, int] = {
    Priority.URGENT: 400,
    Priority.HIGH: 300,
    Priority.MEDIUM: 200,
    Priority.LOW: 100,
}


class EventType(StrEnum):
    """Timeline events rendered in the case drawer (PRD 19)."""

    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    AUTO_ASSIGNED = "AUTO_ASSIGNED"
    REASSIGNED = "REASSIGNED"
    UNASSIGNED = "UNASSIGNED"
    TRANSFERRED = "TRANSFERRED"
    LOCKED = "LOCKED"
    LOCK_RENEWED = "LOCK_RENEWED"
    LOCK_EXPIRED = "LOCK_EXPIRED"
    RELEASED = "RELEASED"
    STATUS_CHANGED = "STATUS_CHANGED"
    STARTED = "STARTED"
    PLACED_ON_HOLD = "PLACED_ON_HOLD"
    COMMENTED = "COMMENTED"
    COMMENT_EDITED = "COMMENT_EDITED"
    COMMENT_DELETED = "COMMENT_DELETED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"
    DUE_AT_OVERRIDDEN = "DUE_AT_OVERRIDDEN"
    SYNCED = "SYNCED"
    BULK_UPDATED = "BULK_UPDATED"


class WorkItem(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """An operational case."""

    __tablename__ = "work_items"
    __table_args__ = (
        # Idempotency key for upstream sync: one work item per source record.
        UniqueConstraint(
            "tenant_id", "source_app", "source_key", name="uq_work_items_tenant_source_key"
        ),
        CheckConstraint(
            "locked_until IS NULL OR locked_by_user_id IS NOT NULL", name="lock_owner_required"
        ),
        Index("ix_work_items_tenant_status", "tenant_id", "status"),
        Index("ix_work_items_tenant_assignee_status", "tenant_id", "assigned_user_id", "status"),
        Index("ix_work_items_tenant_queue_status", "tenant_id", "queue_code", "status"),
        Index("ix_work_items_tenant_source", "tenant_id", "source_app"),
        Index("ix_work_items_tenant_due_at", "tenant_id", "due_at"),
        Index("ix_work_items_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_work_items_tenant_external_ref", "tenant_id", "external_ref"),
        Index(
            "ix_work_items_tenant_lock_owner",
            "tenant_id",
            "locked_by_user_id",
            "locked_until",
        ),
    )

    # --- source identity -------------------------------------------------
    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True), nullable=False
    )
    #: Stable upstream identifier used for idempotent upsert (e.g. ``LOS:APP123``).
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deep_link: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- customer (minimal, masked on the way out) -----------------------
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_mobile_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- ownership -------------------------------------------------------
    queue_code: Mapped[str] = mapped_column(String(64), nullable=False)
    department_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[WorkStatus] = mapped_column(
        Enum(WorkStatus, name="work_status", native_enum=True),
        nullable=False,
        default=WorkStatus.OPEN,
    )
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, name="work_priority", native_enum=True),
        nullable=False,
        default=Priority.MEDIUM,
    )

    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_user_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    #: True when a human picked the assignee. Sync must never overwrite this.
    assignment_is_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    allocation_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- locking (PRD 18) ------------------------------------------------
    locked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    locked_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- sla -------------------------------------------------------------
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sla_policies.id", ondelete="SET NULL"), nullable=True
    )
    sla_status: Mapped[SlaStatus] = mapped_column(
        Enum(SlaStatus, name="sla_status", native_enum=True),
        nullable=False,
        default=SlaStatus.NOT_STARTED,
    )
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- lifecycle -------------------------------------------------------
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    closure_outcome: Mapped[str | None] = mapped_column(String(120), nullable=True)
    closure_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Source controlled fields live here and may be overwritten by sync.
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Ops controlled fields are never touched by sync (PRD Rule 6).
    ops_fields_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sync_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_locked(self) -> bool:
        return self.locked_by_user_id is not None and self.locked_until is not None

    def lock_is_active(self, moment: datetime) -> bool:
        return self.is_locked and bool(self.locked_until and self.locked_until > moment)

    def active_lock_expiry(self, moment: datetime) -> datetime | None:
        """The expiry of a live lock at ``moment``, or ``None``.

        A lock is live when the holder and expiry are both set and the expiry is
        still in the future. Returning the expiry (rather than a bool) lets the
        caller report who holds it without re-asserting the invariant.
        """
        if not self.is_locked or self.locked_until is None:
            return None
        return self.locked_until if self.locked_until > moment else None


class WorkItemEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Append-only timeline entry for a work item (PRD 19).

    Never updated, never deleted. ``before``/``after`` carry only non-PII
    operational values.
    """

    __tablename__ = "work_item_events"
    __table_args__ = (
        Index("ix_work_item_events_tenant_item_created", "tenant_id", "work_item_id", "created_at"),
        Index("ix_work_item_events_tenant_actor", "tenant_id", "actor_user_id"),
        Index("ix_work_item_events_tenant_type", "tenant_id", "event_type"),
    )

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="work_event_type", native_enum=True), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    from_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Correlates an event with the audit row describing the same action.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CaseComment(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Append-only case comment (PRD 21)."""

    __tablename__ = "case_comments"
    __table_args__ = (
        Index("ix_case_comments_tenant_item_created", "tenant_id", "work_item_id", "created_at"),
        Index("ix_case_comments_tenant_author", "tenant_id", "author_user_id"),
    )

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(200), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    #: Set when a privileged administrator deletes the comment. The row stays.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    original_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
