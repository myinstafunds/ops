"""Notifications, integration health and sync run history.

These tables exist in Release 0 so the schema is stable, but the endpoints and
workers that populate them arrive with Releases 2-4.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
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


class NotificationType(StrEnum):
    CASE_ASSIGNED = "CASE_ASSIGNED"
    CASE_TRANSFERRED = "CASE_TRANSFERRED"
    CASE_LOCK_EXPIRING = "CASE_LOCK_EXPIRING"
    SLA_BREACHED = "SLA_BREACHED"
    LEAVE_APPROVED = "LEAVE_APPROVED"
    LEAVE_REJECTED = "LEAVE_REJECTED"
    EXPORT_READY = "EXPORT_READY"
    EXPORT_REJECTED = "EXPORT_REJECTED"
    INTEGRATION_FAILURE = "INTEGRATION_FAILURE"
    QUEUE_NO_AVAILABLE_USERS = "QUEUE_NO_AVAILABLE_USERS"
    ASSIGNMENT_FAILED = "ASSIGNMENT_FAILED"


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """In-app notification. Carries no more customer data than the recipient
    is already authorised to see (PRD 32)."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "ix_notifications_tenant_recipient_unread", "tenant_id", "recipient_user_id", "is_read"
        ),
        Index("ix_notifications_tenant_created", "tenant_id", "created_at"),
    )

    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_users.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name="notification_type", native_enum=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(String(600), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    link_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationHealthStatus(StrEnum):
    """Never hardcoded - always derived from observed probes (PRD 25)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    ISOLATED = "ISOLATED"


class IntegrationHealth(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Observed state of an upstream dependency."""

    __tablename__ = "integration_health"
    __table_args__ = (
        Index("ix_integration_health_tenant_source", "tenant_id", "source_app"),
        # One health row per upstream per tenant; the prober upserts into it.
        UniqueConstraint("tenant_id", "source_app", name="uq_integration_health_tenant_source"),
    )

    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True), nullable=False
    )
    status: Mapped[IntegrationHealthStatus] = mapped_column(
        Enum(IntegrationHealthStatus, name="integration_health_status", native_enum=True),
        nullable=False,
        default=IntegrationHealthStatus.ISOLATED,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class SyncRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CaseSyncRun(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """One execution of a LOS/LMS sync job (PRD 23, 24, 43)."""

    __tablename__ = "case_sync_runs"
    __table_args__ = (
        Index("ix_case_sync_runs_tenant_source_started", "tenant_id", "source_app", "started_at"),
    )

    source_app: Mapped[SourceApp] = mapped_column(
        Enum(SourceApp, name="source_app", native_enum=True), nullable=False
    )
    status: Mapped[SyncRunStatus] = mapped_column(
        Enum(SyncRunStatus, name="sync_run_status", native_enum=True),
        nullable=False,
        default=SyncRunStatus.RUNNING,
    )
    job_name: Mapped[str] = mapped_column(String(120), nullable=False)
    queue_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_users.id", ondelete="SET NULL"), nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SCHEDULED")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
