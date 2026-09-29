"""Departments, queues and their membership tables (PRD 12, 13)."""

from __future__ import annotations

import uuid
from datetime import time as dt_time
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime by the registry
    from app.models.user import User


class SourceApp(StrEnum):
    """Which upstream system produced a record."""

    OPS = "OPS"
    LOS = "LOS"
    LMS = "LMS"


class OrgStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class Department(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Operational grouping of people."""

    __tablename__ = "ops_departments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_ops_departments_tenant_code"),
        Index("ix_ops_departments_tenant_status", "tenant_id", "status"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True),
        nullable=False,
        default=SourceApp.OPS,
    )
    status: Mapped[OrgStatus] = mapped_column(
        Enum(OrgStatus, name="org_status", native_enum=True),
        nullable=False,
        default=OrgStatus.ACTIVE,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    memberships: Mapped[list[DepartmentMembership]] = relationship(
        back_populates="department", cascade="all, delete-orphan"
    )


class DepartmentMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Links a user to a department. A user may hold many (PRD 12)."""

    __tablename__ = "department_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "department_id",
            "user_id",
            name="uq_department_memberships_tenant_dept_user",
        ),
        Index("ix_department_memberships_tenant_user", "tenant_id", "user_id"),
        Index("ix_department_memberships_tenant_department", "tenant_id", "department_id"),
    )

    department_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_departments.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_users.id", ondelete="CASCADE"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # DepartmentMembership has its own department FK, so the relationship to
    # Department must be told which of the two to follow.
    department: Mapped[Department] = relationship(
        back_populates="memberships", lazy="joined", foreign_keys=[department_id]
    )
    user: Mapped[User] = relationship(lazy="joined", foreign_keys=[user_id])


class Queue(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """A pool of work with defined eligibility and SLA (PRD 13)."""

    __tablename__ = "ops_queues"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_ops_queues_tenant_code"),
        Index("ix_ops_queues_tenant_status", "tenant_id", "status"),
        Index("ix_ops_queues_tenant_source", "tenant_id", "source_app"),
        CheckConstraint("max_workload_per_user >= 0", name="ck_ops_queues_workload_non_negative"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True),
        nullable=False,
        default=SourceApp.OPS,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_departments.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[OrgStatus] = mapped_column(
        Enum(OrgStatus, name="org_status", native_enum=True),
        nullable=False,
        default=OrgStatus.ACTIVE,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Maximum open work items a single member may hold.
    max_workload_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    #: Higher number wins when workloads are otherwise equal.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    #: Roles permitted to receive work from this queue.
    eligible_roles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    #: Free text product/specialisation tags required for eligibility.
    required_specialisations: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    #: SLA policy applied when a work item does not carry its own due date.
    sla_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sla_policies.id", ondelete="SET NULL"), nullable=True
    )
    #: Sync cadence in minutes for queues fed by LOS/LMS (PRD 24).
    sync_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    department: Mapped[Department | None] = relationship(lazy="joined")
    memberships: Mapped[list[QueueMembership]] = relationship(
        back_populates="queue", cascade="all, delete-orphan"
    )


class QueueMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """A user's eligibility and capacity within a queue."""

    __tablename__ = "queue_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "queue_id", "user_id", name="uq_queue_memberships_tenant_queue_user"
        ),
        Index("ix_queue_memberships_tenant_user_active", "tenant_id", "user_id", "is_active"),
        Index("ix_queue_memberships_tenant_queue_active", "tenant_id", "queue_id", "is_active"),
    )

    queue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_queues.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_users.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Per-queue capacity. Falls back to the user capacity when null.
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Per-queue daily allocation cap.
    daily_max_allocations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    specialisation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shift_start: Mapped[dt_time | None] = mapped_column(Time, nullable=True)
    shift_end: Mapped[dt_time | None] = mapped_column(Time, nullable=True)
    #: Weekday numbers 0..6 with Monday == 0, matching ``datetime.weekday()``.
    shift_days: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)

    queue: Mapped[Queue] = relationship(back_populates="memberships", lazy="joined")
    user: Mapped[User] = relationship(lazy="joined")
