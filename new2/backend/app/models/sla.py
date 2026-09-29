"""SLA policies (PRD 22).

``due_at`` is normally derived from the queue's SLA policy rather than being
typed in by hand. An authorised user may override it, and the override is
audited.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.org import SourceApp


class SlaStatus(StrEnum):
    """SLA state of a single work item (PRD 22)."""

    NOT_STARTED = "NOT_STARTED"
    ON_TRACK = "ON_TRACK"
    DUE_SOON = "DUE_SOON"
    BREACHED = "BREACHED"
    COMPLETED = "COMPLETED"


class SlaBreachAction(StrEnum):
    NONE = "NONE"
    NOTIFY_ASSIGNEE = "NOTIFY_ASSIGNEE"
    NOTIFY_MANAGER = "NOTIFY_MANAGER"
    ESCALATE = "ESCALATE"


class SlaPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """A named response-time target."""

    __tablename__ = "sla_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_sla_policies_tenant_code"),
        # A warning threshold later than the deadline could never fire.
        CheckConstraint(
            "warning_minutes <= target_minutes", name="ck_sla_policies_warning_before_target"
        ),
        CheckConstraint("target_minutes > 0", name="ck_sla_policies_target_positive"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True),
        nullable=False,
        default=SourceApp.OPS,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Minutes before the deadline at which the item becomes DUE_SOON.
    warning_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    #: Business-hours-only clocks ignore nights and weekends.
    business_hours_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    breach_action: Mapped[SlaBreachAction] = mapped_column(
        Enum(SlaBreachAction, name="sla_breach_action", native_enum=True),
        nullable=False,
        default=SlaBreachAction.NOTIFY_MANAGER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class QueueSlaPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Explicit per-queue override of the default SLA policy."""

    __tablename__ = "queue_sla_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "queue_id", "sla_policy_id", name="uq_queue_sla_policies_tenant"
        ),
    )

    queue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_queues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sla_policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sla_policies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default_for_queue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sla_policy: Mapped[SlaPolicy] = relationship(lazy="joined")
