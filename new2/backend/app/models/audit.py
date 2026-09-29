"""Append-only audit log (PRD 28).

Rows are never updated or deleted by application code. Writes go through
``app.audit.writer.record`` so every call site is uniform.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AuditAction(StrEnum):
    """Every audited action (PRD 28)."""

    # identity / access
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_ACTIVATED = "USER_ACTIVATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    ROLE_PERMISSION_CHANGED = "ROLE_PERMISSION_CHANGED"

    # organisation
    DEPARTMENT_CREATED = "DEPARTMENT_CREATED"
    DEPARTMENT_UPDATED = "DEPARTMENT_UPDATED"
    DEPARTMENT_MEMBERSHIP_CREATED = "DEPARTMENT_MEMBERSHIP_CREATED"
    DEPARTMENT_MEMBERSHIP_UPDATED = "DEPARTMENT_MEMBERSHIP_UPDATED"
    QUEUE_CREATED = "QUEUE_CREATED"
    QUEUE_UPDATED = "QUEUE_UPDATED"
    QUEUE_MEMBERSHIP_CREATED = "QUEUE_MEMBERSHIP_CREATED"
    QUEUE_MEMBERSHIP_UPDATED = "QUEUE_MEMBERSHIP_UPDATED"

    # availability
    AVAILABILITY_CREATED = "AVAILABILITY_CREATED"
    AVAILABILITY_UPDATED = "AVAILABILITY_UPDATED"
    AVAILABILITY_APPROVED = "AVAILABILITY_APPROVED"
    AVAILABILITY_REJECTED = "AVAILABILITY_REJECTED"
    AVAILABILITY_CANCELLED = "AVAILABILITY_CANCELLED"

    # work
    WORK_ITEM_CREATED = "WORK_ITEM_CREATED"
    WORK_ITEM_ASSIGNED = "WORK_ITEM_ASSIGNED"
    WORK_ITEM_AUTO_ASSIGNED = "WORK_ITEM_AUTO_ASSIGNED"
    WORK_ITEM_UNASSIGNED = "WORK_ITEM_UNASSIGNED"
    WORK_ITEM_TRANSFERRED = "WORK_ITEM_TRANSFERRED"
    WORK_ITEM_STATUS_CHANGED = "WORK_ITEM_STATUS_CHANGED"
    WORK_ITEM_LOCKED = "WORK_ITEM_LOCKED"
    WORK_ITEM_HEARTBEAT = "WORK_ITEM_HEARTBEAT"
    WORK_ITEM_RELEASED = "WORK_ITEM_RELEASED"
    WORK_ITEM_CLOSED = "WORK_ITEM_CLOSED"
    WORK_ITEM_COMMENTED = "WORK_ITEM_COMMENTED"
    WORK_ITEM_COMMENT_EDITED = "WORK_ITEM_COMMENT_EDITED"
    WORK_ITEM_COMMENT_DELETED = "WORK_ITEM_COMMENT_DELETED"
    WORK_ITEM_DUE_AT_OVERRIDDEN = "WORK_ITEM_DUE_AT_OVERRIDDEN"
    WORK_ITEM_BULK_UPDATED = "WORK_ITEM_BULK_UPDATED"
    WORK_ITEM_SYNCED = "WORK_ITEM_SYNCED"

    # communication
    COMMUNICATION_CREATED = "COMMUNICATION_CREATED"
    COMMUNICATION_STATUS_UPDATED = "COMMUNICATION_STATUS_UPDATED"

    # integrations / exports
    EXPORT_REQUESTED = "EXPORT_REQUESTED"
    EXPORT_APPROVED = "EXPORT_APPROVED"
    EXPORT_REJECTED = "EXPORT_REJECTED"
    EXPORT_COMPLETED = "EXPORT_COMPLETED"
    INTEGRATION_SETTING_CHANGED = "INTEGRATION_SETTING_CHANGED"
    WRITEBACK_ATTEMPTED = "WRITEBACK_ATTEMPTED"
    WRITEBACK_SUCCEEDED = "WRITEBACK_SUCCEEDED"
    WRITEBACK_FAILED = "WRITEBACK_FAILED"

    # system
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    ACCESS_DENIED = "ACCESS_DENIED"
    SETTINGS_UPDATED = "SETTINGS_UPDATED"


class EntityType(StrEnum):
    USER = "USER"
    USER_ROLE = "USER_ROLE"
    DEPARTMENT = "DEPARTMENT"
    DEPARTMENT_MEMBERSHIP = "DEPARTMENT_MEMBERSHIP"
    QUEUE = "QUEUE"
    QUEUE_MEMBERSHIP = "QUEUE_MEMBERSHIP"
    AVAILABILITY = "AVAILABILITY"
    WORK_ITEM = "WORK_ITEM"
    WORK_ITEM_EVENT = "WORK_ITEM_EVENT"
    CASE_COMMENT = "CASE_COMMENT"
    COMMUNICATION = "COMMUNICATION"
    EXPORT_REQUEST = "EXPORT_REQUEST"
    INTEGRATION = "INTEGRATION"
    SETTINGS = "SETTINGS"
    AUTH = "AUTH"
    SYSTEM = "SYSTEM"


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Immutable record of an important action."""

    __tablename__ = "ops_audit_log"
    __table_args__ = (
        Index("ix_ops_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("ix_ops_audit_log_tenant_actor", "tenant_id", "actor_user_id", "created_at"),
        Index("ix_ops_audit_log_tenant_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_ops_audit_log_tenant_action", "tenant_id", "action", "created_at"),
        Index("ix_ops_audit_log_tenant_trace", "tenant_id", "trace_id"),
    )

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action", native_enum=True), nullable=False
    )
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="audit_entity_type", native_enum=True), nullable=False
    )
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Serialised state before and after. Never contains raw PII.
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    changed_fields: Mapped[str | None] = mapped_column(Text, nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Set for automated actions so they are distinguishable from human ones.
    is_system_action: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
