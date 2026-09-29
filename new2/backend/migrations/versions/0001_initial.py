"""Initial Ops Control Center schema (Release 0 + 1).

Creates the operational control plane: users, org structure, queues, SLA
policies, availability, work items with locking, the event trail, the
append-only audit log and the integration health tables.

Design notes that matter later:

* Every operational table carries ``tenant_id`` and every index leads with it,
  so a tenant scoped query is always index served (PRD Rule 4).
* Enums are native PostgreSQL types. Adding a value needs a migration, not a
  code change, which is the point.
* No column stores a password or any other credential material.
* ``work_items.customer_mobile`` exists only so the masking rule in
  ``app.security.masking`` can be applied on the way out; it is never returned
  unmasked.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# enum types
#
# Each type is created exactly once at the top of ``upgrade()``; the column
# definitions below reference it with ``create_type=False`` so neither online
# nor offline (``alembic upgrade --sql``) runs emit a duplicate CREATE TYPE.
# ---------------------------------------------------------------------------
def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


SOURCE_APP = _enum("source_app", "OPS", "LOS", "LMS")
ORG_STATUS = _enum("org_status", "ACTIVE", "INACTIVE")
OPERATIONAL_STATUS = _enum("operational_status", "ACTIVE", "INACTIVE", "SUSPENDED")
SLA_BREACH_ACTION = _enum(
    "sla_breach_action", "NONE", "NOTIFY_ASSIGNEE", "NOTIFY_MANAGER", "ESCALATE"
)
SLA_STATUS = _enum(
    "sla_status", "NOT_STARTED", "ON_TRACK", "DUE_SOON", "BREACHED", "COMPLETED"
)
AVAILABILITY_TYPE = _enum(
    "availability_type", "LEAVE", "ABSENT", "BREAK", "TRAINING", "OFFLINE"
)
AVAILABILITY_REQUEST_STATUS = _enum(
    "availability_request_status", "REQUESTED", "APPROVED", "REJECTED", "CANCELLED"
)
WORK_STATUS = _enum(
    "work_status", "OPEN", "ASSIGNED", "IN_PROGRESS", "ON_HOLD", "CLOSED", "CANCELLED"
)
WORK_PRIORITY = _enum("work_priority", "LOW", "MEDIUM", "HIGH", "URGENT")
WORK_EVENT_TYPE = _enum(
    "work_event_type",
    "CREATED",
    "ASSIGNED",
    "AUTO_ASSIGNED",
    "REASSIGNED",
    "UNASSIGNED",
    "TRANSFERRED",
    "LOCKED",
    "LOCK_RENEWED",
    "LOCK_EXPIRED",
    "RELEASED",
    "STARTED",
    "STATUS_CHANGED",
    "PLACED_ON_HOLD",
    "REOPENED",
    "CLOSED",
    "SYNCED",
    "DUE_AT_OVERRIDDEN",
    "BULK_UPDATED",
    "COMMENTED",
    "COMMENT_EDITED",
    "COMMENT_DELETED",
)
COMMUNICATION_CHANNEL = _enum(
    "communication_channel", "CALL", "SMS", "EMAIL", "WHATSAPP", "RCS"
)
COMMUNICATION_DIRECTION = _enum("communication_direction", "INBOUND", "OUTBOUND")
COMMUNICATION_STATUS = _enum(
    "communication_status",
    "PENDING",
    "SENT",
    "DELIVERED",
    "FAILED",
    "CONNECTED",
    "NO_RESPONSE",
)
CALL_DISPOSITION = _enum(
    "call_disposition",
    "CONNECTED",
    "NO_ANSWER",
    "CALLBACK_REQUESTED",
    "PAYMENT_PROMISED",
    "REFUSED",
    "NOT_INTERESTED",
    "WRONG_NUMBER",
    "RESOLVED",
)
AUDIT_ACTION = _enum(
    "audit_action",
    "LOGIN_SUCCEEDED",
    "LOGIN_FAILED",
    "LOGOUT",
    "USER_CREATED",
    "USER_UPDATED",
    "USER_ACTIVATED",
    "USER_DEACTIVATED",
    "USER_ROLE_CHANGED",
    "ROLE_PERMISSION_CHANGED",
    "DEPARTMENT_CREATED",
    "DEPARTMENT_UPDATED",
    "DEPARTMENT_MEMBERSHIP_CREATED",
    "DEPARTMENT_MEMBERSHIP_UPDATED",
    "QUEUE_CREATED",
    "QUEUE_UPDATED",
    "QUEUE_MEMBERSHIP_CREATED",
    "QUEUE_MEMBERSHIP_UPDATED",
    "AVAILABILITY_CREATED",
    "AVAILABILITY_UPDATED",
    "AVAILABILITY_APPROVED",
    "AVAILABILITY_REJECTED",
    "AVAILABILITY_CANCELLED",
    "WORK_ITEM_CREATED",
    "WORK_ITEM_ASSIGNED",
    "WORK_ITEM_AUTO_ASSIGNED",
    "WORK_ITEM_UNASSIGNED",
    "WORK_ITEM_TRANSFERRED",
    "WORK_ITEM_LOCKED",
    "WORK_ITEM_RELEASED",
    "WORK_ITEM_HEARTBEAT",
    "WORK_ITEM_STATUS_CHANGED",
    "WORK_ITEM_CLOSED",
    "WORK_ITEM_COMMENTED",
    "WORK_ITEM_COMMENT_EDITED",
    "WORK_ITEM_COMMENT_DELETED",
    "WORK_ITEM_DUE_AT_OVERRIDDEN",
    "WORK_ITEM_BULK_UPDATED",
    "WORK_ITEM_SYNCED",
    "COMMUNICATION_CREATED",
    "COMMUNICATION_STATUS_UPDATED",
    "INTEGRATION_SETTING_CHANGED",
    "EXPORT_REQUESTED",
    "EXPORT_APPROVED",
    "EXPORT_REJECTED",
    "EXPORT_COMPLETED",
    "WRITEBACK_ATTEMPTED",
    "WRITEBACK_SUCCEEDED",
    "WRITEBACK_FAILED",
    "SETTINGS_UPDATED",
    "ACCESS_DENIED",
)
AUDIT_ENTITY_TYPE = _enum(
    "audit_entity_type",
    "AUTH",
    "USER",
    "USER_ROLE",
    "DEPARTMENT",
    "DEPARTMENT_MEMBERSHIP",
    "QUEUE",
    "QUEUE_MEMBERSHIP",
    "AVAILABILITY",
    "WORK_ITEM",
    "WORK_ITEM_EVENT",
    "CASE_COMMENT",
    "COMMUNICATION",
    "INTEGRATION",
    "EXPORT_REQUEST",
    "SETTINGS",
    "SYSTEM",
)
INTEGRATION_HEALTH_STATUS = _enum(
    "integration_health_status", "HEALTHY", "DEGRADED", "FAILED", "ISOLATED"
)
SYNC_RUN_STATUS = _enum(
    "sync_run_status", "RUNNING", "SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED", "CANCELLED"
)
NOTIFICATION_TYPE = _enum(
    "notification_type",
    "CASE_ASSIGNED",
    "CASE_TRANSFERRED",
    "CASE_LOCK_EXPIRING",
    "SLA_BREACHED",
    "QUEUE_NO_AVAILABLE_USERS",
    "LEAVE_APPROVED",
    "LEAVE_REJECTED",
    "ASSIGNMENT_FAILED",
    "INTEGRATION_FAILURE",
    "EXPORT_READY",
    "EXPORT_REJECTED",
)

#: (name, values) pairs - the authoritative list of enum types in this schema.
ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("source_app", ("OPS", "LOS", "LMS")),
    ("org_status", ("ACTIVE", "INACTIVE")),
    ("operational_status", ("ACTIVE", "INACTIVE", "SUSPENDED")),
    (
        "sla_breach_action",
        ("NONE", "NOTIFY_ASSIGNEE", "NOTIFY_MANAGER", "ESCALATE"),
    ),
    ("sla_status", ("NOT_STARTED", "ON_TRACK", "DUE_SOON", "BREACHED", "COMPLETED")),
    ("availability_type", ("LEAVE", "ABSENT", "BREAK", "TRAINING", "OFFLINE")),
    ("availability_request_status", ("REQUESTED", "APPROVED", "REJECTED", "CANCELLED")),
    ("work_status", ("OPEN", "ASSIGNED", "IN_PROGRESS", "ON_HOLD", "CLOSED", "CANCELLED")),
    ("work_priority", ("LOW", "MEDIUM", "HIGH", "URGENT")),
    (
        "work_event_type",
        (
            "CREATED",
            "ASSIGNED",
            "AUTO_ASSIGNED",
            "REASSIGNED",
            "UNASSIGNED",
            "TRANSFERRED",
            "LOCKED",
            "LOCK_RENEWED",
            "LOCK_EXPIRED",
            "RELEASED",
            "STARTED",
            "STATUS_CHANGED",
            "PLACED_ON_HOLD",
            "REOPENED",
            "CLOSED",
            "SYNCED",
            "DUE_AT_OVERRIDDEN",
            "BULK_UPDATED",
            "COMMENTED",
            "COMMENT_EDITED",
            "COMMENT_DELETED",
        ),
    ),
    ("communication_channel", ("CALL", "SMS", "EMAIL", "WHATSAPP", "RCS")),
    ("communication_direction", ("INBOUND", "OUTBOUND")),
    (
        "communication_status",
        ("PENDING", "SENT", "DELIVERED", "FAILED", "CONNECTED", "NO_RESPONSE"),
    ),
    (
        "call_disposition",
        (
            "CONNECTED",
            "NO_ANSWER",
            "CALLBACK_REQUESTED",
            "PAYMENT_PROMISED",
            "REFUSED",
            "NOT_INTERESTED",
            "WRONG_NUMBER",
            "RESOLVED",
        ),
    ),
    (
        "audit_action",
        (
            "LOGIN_SUCCEEDED",
            "LOGIN_FAILED",
            "LOGOUT",
            "USER_CREATED",
            "USER_UPDATED",
            "USER_ACTIVATED",
            "USER_DEACTIVATED",
            "USER_ROLE_CHANGED",
            "ROLE_PERMISSION_CHANGED",
            "DEPARTMENT_CREATED",
            "DEPARTMENT_UPDATED",
            "DEPARTMENT_MEMBERSHIP_CREATED",
            "DEPARTMENT_MEMBERSHIP_UPDATED",
            "QUEUE_CREATED",
            "QUEUE_UPDATED",
            "QUEUE_MEMBERSHIP_CREATED",
            "QUEUE_MEMBERSHIP_UPDATED",
            "AVAILABILITY_CREATED",
            "AVAILABILITY_UPDATED",
            "AVAILABILITY_APPROVED",
            "AVAILABILITY_REJECTED",
            "AVAILABILITY_CANCELLED",
            "WORK_ITEM_CREATED",
            "WORK_ITEM_ASSIGNED",
            "WORK_ITEM_AUTO_ASSIGNED",
            "WORK_ITEM_UNASSIGNED",
            "WORK_ITEM_TRANSFERRED",
            "WORK_ITEM_LOCKED",
            "WORK_ITEM_RELEASED",
            "WORK_ITEM_HEARTBEAT",
            "WORK_ITEM_STATUS_CHANGED",
            "WORK_ITEM_CLOSED",
            "WORK_ITEM_COMMENTED",
            "WORK_ITEM_COMMENT_EDITED",
            "WORK_ITEM_COMMENT_DELETED",
            "WORK_ITEM_DUE_AT_OVERRIDDEN",
            "WORK_ITEM_BULK_UPDATED",
            "WORK_ITEM_SYNCED",
            "COMMUNICATION_CREATED",
            "COMMUNICATION_STATUS_UPDATED",
            "INTEGRATION_SETTING_CHANGED",
            "EXPORT_REQUESTED",
            "EXPORT_APPROVED",
            "EXPORT_REJECTED",
            "EXPORT_COMPLETED",
            "WRITEBACK_ATTEMPTED",
            "WRITEBACK_SUCCEEDED",
            "WRITEBACK_FAILED",
            "SETTINGS_UPDATED",
            "ACCESS_DENIED",
        ),
    ),
    (
        "audit_entity_type",
        (
            "AUTH",
            "USER",
            "USER_ROLE",
            "DEPARTMENT",
            "DEPARTMENT_MEMBERSHIP",
            "QUEUE",
            "QUEUE_MEMBERSHIP",
            "AVAILABILITY",
            "WORK_ITEM",
            "WORK_ITEM_EVENT",
            "CASE_COMMENT",
            "COMMUNICATION",
            "INTEGRATION",
            "EXPORT_REQUEST",
            "SETTINGS",
            "SYSTEM",
        ),
    ),
    ("integration_health_status", ("HEALTHY", "DEGRADED", "FAILED", "ISOLATED")),
    (
        "sync_run_status",
        ("RUNNING", "SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED", "CANCELLED"),
    ),
    (
        "notification_type",
        (
            "CASE_ASSIGNED",
            "CASE_TRANSFERRED",
            "CASE_LOCK_EXPIRING",
            "SLA_BREACHED",
            "QUEUE_NO_AVAILABLE_USERS",
            "LEAVE_APPROVED",
            "LEAVE_REJECTED",
            "ASSIGNMENT_FAILED",
            "INTEGRATION_FAILURE",
            "EXPORT_READY",
            "EXPORT_REJECTED",
        ),
    ),
)

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TZ = sa.DateTime(timezone=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            TZ,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            TZ,
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUM_TYPES:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    # ---------------------------------------------------------------- users
    op.create_table(
        "ops_users",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("bre_user_id", sa.String(128), nullable=True),
        sa.Column("username", sa.String(150), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("status", OPERATIONAL_STATUS, nullable=False),
        sa.Column("is_available", sa.Boolean, nullable=False),
        sa.Column("availability_ends_at", TZ, nullable=True),
        sa.Column("capacity", sa.Integer, nullable=False),
        sa.Column("priority_weight", sa.Integer, nullable=False),
        sa.Column("daily_allocation_limit", sa.Integer, nullable=False),
        sa.Column("job_title", sa.String(150), nullable=True),
        sa.Column("phone_masked", sa.String(32), nullable=True),
        sa.Column("specialisations", sa.Text, nullable=True),
        sa.Column("bre_roles", sa.Text, nullable=True),
        sa.Column("last_login_at", TZ, nullable=True),
        sa.Column("deactivated_at", TZ, nullable=True),
        sa.Column("deactivated_reason", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ops_users"),
        sa.UniqueConstraint(
            "tenant_id", "bre_user_id", name="uq_ops_users_tenant_bre_user"
        ),
        sa.UniqueConstraint("tenant_id", "username", name="uq_ops_users_tenant_username"),
    )
    op.create_index("ix_ops_users_created_at", "ops_users", ["created_at"])
    op.create_index("ix_ops_users_tenant_id", "ops_users", ["tenant_id"])
    op.create_index("ix_ops_users_tenant_status", "ops_users", ["tenant_id", "status"])
    op.create_index(
        "ix_ops_users_tenant_display_name", "ops_users", ["tenant_id", "display_name"]
    )

    op.create_table(
        "user_roles",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["ops_users.id"], name="fk_user_roles_user_id_ops_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_roles"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "role", name="uq_user_roles_tenant_user_role"
        ),
    )
    op.create_index("ix_user_roles_created_at", "user_roles", ["created_at"])
    op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"])
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("ix_user_roles_tenant_role", "user_roles", ["tenant_id", "role"])

    # ---------------------------------------------------------- departments
    op.create_table(
        "ops_departments",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("status", ORG_STATUS, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ops_departments"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_ops_departments_tenant_code"),
    )
    op.create_index("ix_ops_departments_created_at", "ops_departments", ["created_at"])
    op.create_index("ix_ops_departments_tenant_id", "ops_departments", ["tenant_id"])
    op.create_index(
        "ix_ops_departments_tenant_status", "ops_departments", ["tenant_id", "status"]
    )

    op.create_table(
        "department_memberships",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("department_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["ops_departments.id"],
            name="fk_department_memberships_department_id_ops_departments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["ops_users.id"],
            name="fk_department_memberships_user_id_ops_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_department_memberships"),
        sa.UniqueConstraint(
            "tenant_id",
            "department_id",
            "user_id",
            name="uq_department_memberships_tenant_dept_user",
        ),
    )
    op.create_index(
        "ix_department_memberships_created_at", "department_memberships", ["created_at"]
    )
    op.create_index(
        "ix_department_memberships_tenant_id", "department_memberships", ["tenant_id"]
    )
    op.create_index(
        "ix_department_memberships_tenant_department",
        "department_memberships",
        ["tenant_id", "department_id"],
    )
    op.create_index(
        "ix_department_memberships_tenant_user", "department_memberships", ["tenant_id", "user_id"]
    )

    # ---------------------------------------------------------- sla policies
    op.create_table(
        "sla_policies",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("target_minutes", sa.Integer, nullable=False),
        sa.Column("warning_minutes", sa.Integer, nullable=False),
        sa.Column("business_hours_only", sa.Boolean, nullable=False),
        sa.Column("breach_action", SLA_BREACH_ACTION, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sla_policies"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_sla_policies_tenant_code"),
        sa.CheckConstraint(
            "warning_minutes <= target_minutes", name="ck_sla_policies_warning_before_target"
        ),
        sa.CheckConstraint("target_minutes > 0", name="ck_sla_policies_target_positive"),
    )
    op.create_index("ix_sla_policies_created_at", "sla_policies", ["created_at"])
    op.create_index("ix_sla_policies_tenant_id", "sla_policies", ["tenant_id"])

    # ---------------------------------------------------------------- queues
    op.create_table(
        "ops_queues",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("department_id", UUID, nullable=True),
        sa.Column("status", ORG_STATUS, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("max_workload_per_user", sa.Integer, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("eligible_roles", JSONB, nullable=True),
        sa.Column("required_specialisations", JSONB, nullable=True),
        sa.Column("sla_policy_id", UUID, nullable=True),
        sa.Column("sync_interval_minutes", sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["ops_departments.id"],
            name="fk_ops_queues_department_id_ops_departments",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sla_policy_id"],
            ["sla_policies.id"],
            name="fk_ops_queues_sla_policy_id_sla_policies",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ops_queues"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_ops_queues_tenant_code"),
        sa.CheckConstraint(
            "max_workload_per_user >= 0", name="ck_ops_queues_workload_non_negative"
        ),
    )
    op.create_index("ix_ops_queues_created_at", "ops_queues", ["created_at"])
    op.create_index("ix_ops_queues_tenant_id", "ops_queues", ["tenant_id"])
    op.create_index("ix_ops_queues_tenant_status", "ops_queues", ["tenant_id", "status"])
    op.create_index(
        "ix_ops_queues_tenant_source", "ops_queues", ["tenant_id", "source_app"]
    )

    op.create_table(
        "queue_memberships",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("queue_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("capacity", sa.Integer, nullable=True),
        sa.Column("priority_weight", sa.Integer, nullable=False),
        sa.Column("daily_max_allocations", sa.Integer, nullable=True),
        sa.Column("specialisation", sa.String(120), nullable=True),
        sa.Column("shift_start", sa.Time(), nullable=True),
        sa.Column("shift_end", sa.Time(), nullable=True),
        sa.Column("shift_days", JSONB, nullable=True),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["ops_queues.id"],
            name="fk_queue_memberships_queue_id_ops_queues",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["ops_users.id"],
            name="fk_queue_memberships_user_id_ops_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queue_memberships"),
        sa.UniqueConstraint(
            "tenant_id", "queue_id", "user_id", name="uq_queue_memberships_tenant_queue_user"
        ),
    )
    op.create_index(
        "ix_queue_memberships_created_at", "queue_memberships", ["created_at"]
    )
    op.create_index("ix_queue_memberships_tenant_id", "queue_memberships", ["tenant_id"])
    op.create_index(
        "ix_queue_memberships_tenant_queue_active",
        "queue_memberships",
        ["tenant_id", "queue_id", "is_active"],
    )
    op.create_index(
        "ix_queue_memberships_tenant_user_active",
        "queue_memberships",
        ["tenant_id", "user_id", "is_active"],
    )

    op.create_table(
        "queue_sla_policies",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("queue_id", UUID, nullable=False),
        sa.Column("sla_policy_id", UUID, nullable=False),
        sa.Column("target_minutes", sa.Integer, nullable=True),
        sa.Column("warning_minutes", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_default_for_queue", sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["ops_queues.id"],
            name="fk_queue_sla_policies_queue_id_ops_queues",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sla_policy_id"],
            ["sla_policies.id"],
            name="fk_queue_sla_policies_sla_policy_id_sla_policies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queue_sla_policies"),
        sa.UniqueConstraint(
            "tenant_id", "queue_id", "sla_policy_id", name="uq_queue_sla_policies_tenant"
        ),
    )
    op.create_index("ix_queue_sla_policies_created_at", "queue_sla_policies", ["created_at"])
    op.create_index("ix_queue_sla_policies_tenant_id", "queue_sla_policies", ["tenant_id"])
    op.create_index("ix_queue_sla_policies_queue_id", "queue_sla_policies", ["queue_id"])
    op.create_index(
        "ix_queue_sla_policies_sla_policy_id", "queue_sla_policies", ["sla_policy_id"]
    )

    # ----------------------------------------------------------- availability
    op.create_table(
        "user_availability",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("availability_type", AVAILABILITY_TYPE, nullable=False),
        sa.Column("status", AVAILABILITY_REQUEST_STATUS, nullable=False),
        sa.Column("start_at", TZ, nullable=False),
        sa.Column("end_at", TZ, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True),
        sa.Column("decided_by_user_id", UUID, nullable=True),
        sa.Column("decided_by_name", sa.String(200), nullable=True),
        sa.Column("decided_at", TZ, nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
        sa.Column("edit_count", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["ops_users.id"],
            name="fk_user_availability_user_id_ops_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["ops_users.id"],
            name="fk_user_availability_created_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["ops_users.id"],
            name="fk_user_availability_decided_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_availability"),
        # A window must have positive duration.
        sa.CheckConstraint("end_at > start_at", name="ck_user_availability_end_after_start"),
        # Self approval is impossible at the storage layer, not just the service.
        sa.CheckConstraint(
            "created_by_user_id IS NULL OR decided_by_user_id IS DISTINCT FROM created_by_user_id",
            name="ck_user_availability_no_self_approval",
        ),
    )
    op.create_index(
        "ix_user_availability_created_at", "user_availability", ["created_at"]
    )
    op.create_index("ix_user_availability_tenant_id", "user_availability", ["tenant_id"])
    op.create_index(
        "ix_user_availability_tenant_status", "user_availability", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_user_availability_tenant_user_window",
        "user_availability",
        ["tenant_id", "user_id", "status", "start_at", "end_at"],
    )

    # ------------------------------------------------------------ work items
    op.create_table(
        "work_items",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("source_key", sa.String(255), nullable=True),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("deep_link", sa.String(500), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("customer_name", sa.String(200), nullable=True),
        sa.Column("customer_mobile", sa.String(32), nullable=True),
        sa.Column("customer_mobile_masked", sa.String(32), nullable=True),
        sa.Column("queue_code", sa.String(64), nullable=False),
        sa.Column("department_code", sa.String(32), nullable=True),
        sa.Column("status", WORK_STATUS, nullable=False),
        sa.Column("priority", WORK_PRIORITY, nullable=False),
        sa.Column("assigned_user_id", UUID, nullable=True),
        sa.Column("assigned_user_name", sa.String(200), nullable=True),
        sa.Column("assigned_at", TZ, nullable=True),
        sa.Column("assigned_by_user_id", UUID, nullable=True),
        sa.Column(
            "assignment_is_manual", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("allocation_explanation", sa.Text, nullable=True),
        sa.Column("locked_by_user_id", UUID, nullable=True),
        sa.Column("locked_by_name", sa.String(200), nullable=True),
        sa.Column("locked_at", TZ, nullable=True),
        sa.Column("locked_until", TZ, nullable=True),
        sa.Column("lock_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_heartbeat_at", TZ, nullable=True),
        sa.Column("due_at", TZ, nullable=True),
        sa.Column("sla_policy_id", UUID, nullable=True),
        sa.Column("sla_status", SLA_STATUS, nullable=False),
        sa.Column("sla_breached_at", TZ, nullable=True),
        sa.Column(
            "due_at_overridden", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("closed_at", TZ, nullable=True),
        sa.Column("closed_by_user_id", UUID, nullable=True),
        sa.Column("closure_outcome", sa.String(120), nullable=True),
        sa.Column("closure_notes", sa.Text, nullable=True),
        sa.Column("last_activity_at", TZ, nullable=True),
        sa.Column("first_response_at", TZ, nullable=True),
        sa.Column("payload_json", JSONB, nullable=True),
        sa.Column("ops_fields_json", JSONB, nullable=True),
        sa.Column("sync_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_synced_at", TZ, nullable=True),
        sa.ForeignKeyConstraint(
            ["assigned_user_id"],
            ["ops_users.id"],
            name="fk_work_items_assigned_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["ops_users.id"],
            name="fk_work_items_assigned_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["locked_by_user_id"],
            ["ops_users.id"],
            name="fk_work_items_locked_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sla_policy_id"],
            ["sla_policies.id"],
            name="fk_work_items_sla_policy_id_sla_policies",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["closed_by_user_id"],
            ["ops_users.id"],
            name="fk_work_items_closed_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_items"),
        sa.UniqueConstraint(
            "tenant_id", "source_app", "source_key", name="uq_work_items_tenant_source_key"
        ),
        # A live lock always names its holder.
        sa.CheckConstraint(
            "locked_until IS NULL OR locked_by_user_id IS NOT NULL",
            name="ck_work_items_lock_owner_required",
        ),
    )
    op.create_index("ix_work_items_created_at", "work_items", ["created_at"])
    op.create_index("ix_work_items_tenant_id", "work_items", ["tenant_id"])
    op.create_index("ix_work_items_tenant_status", "work_items", ["tenant_id", "status"])
    op.create_index("ix_work_items_tenant_created_at", "work_items", ["tenant_id", "created_at"])
    op.create_index(
        "ix_work_items_tenant_queue_status", "work_items", ["tenant_id", "queue_code", "status"]
    )
    op.create_index(
        "ix_work_items_tenant_assignee_status",
        "work_items",
        ["tenant_id", "assigned_user_id", "status"],
    )
    op.create_index("ix_work_items_tenant_due_at", "work_items", ["tenant_id", "due_at"])
    op.create_index(
        "ix_work_items_tenant_external_ref", "work_items", ["tenant_id", "external_ref"]
    )
    op.create_index(
        "ix_work_items_tenant_lock_owner",
        "work_items",
        ["tenant_id", "locked_by_user_id", "locked_until"],
    )
    op.create_index("ix_work_items_locked_until", "work_items", ["locked_until"])
    op.create_index("ix_work_items_tenant_source", "work_items", ["tenant_id", "source_app"])

    op.create_table(
        "work_item_events",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("work_item_id", UUID, nullable=False),
        sa.Column("event_type", WORK_EVENT_TYPE, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=False),
        sa.Column("from_value", sa.String(255), nullable=True),
        sa.Column("to_value", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_work_item_events_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["ops_users.id"],
            name="fk_work_item_events_actor_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_item_events"),
    )
    op.create_index("ix_work_item_events_created_at", "work_item_events", ["created_at"])
    op.create_index("ix_work_item_events_tenant_id", "work_item_events", ["tenant_id"])
    op.create_index(
        "ix_work_item_events_tenant_item_created",
        "work_item_events",
        ["tenant_id", "work_item_id", "created_at"],
    )
    op.create_index(
        "ix_work_item_events_tenant_type", "work_item_events", ["tenant_id", "event_type"]
    )
    op.create_index(
        "ix_work_item_events_tenant_actor", "work_item_events", ["tenant_id", "actor_user_id"]
    )

    op.create_table(
        "case_comments",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("work_item_id", UUID, nullable=False),
        sa.Column("author_user_id", UUID, nullable=True),
        sa.Column("author_name", sa.String(200), nullable=False),
        sa.Column("comment", sa.Text, nullable=False),
        sa.Column("deleted_at", TZ, nullable=True),
        sa.Column("deleted_by_user_id", UUID, nullable=True),
        sa.Column("delete_reason", sa.Text, nullable=True),
        sa.Column("edit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("original_comment", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_case_comments_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["ops_users.id"],
            name="fk_case_comments_author_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_user_id"],
            ["ops_users.id"],
            name="fk_case_comments_deleted_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_case_comments"),
    )
    op.create_index("ix_case_comments_created_at", "case_comments", ["created_at"])
    op.create_index("ix_case_comments_tenant_id", "case_comments", ["tenant_id"])
    op.create_index(
        "ix_case_comments_tenant_item_created",
        "case_comments",
        ["tenant_id", "work_item_id", "created_at"],
    )
    op.create_index(
        "ix_case_comments_tenant_author", "case_comments", ["tenant_id", "author_user_id"]
    )

    # -------------------------------------------------------- communications
    op.create_table(
        "communication_events",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("work_item_id", UUID, nullable=True),
        sa.Column("source_app", sa.String(16), nullable=True),
        sa.Column("channel", COMMUNICATION_CHANNEL, nullable=False),
        sa.Column("direction", COMMUNICATION_DIRECTION, nullable=False),
        sa.Column("status", COMMUNICATION_STATUS, nullable=False),
        sa.Column("customer_reference", sa.String(128), nullable=True),
        sa.Column("customer_name", sa.String(200), nullable=True),
        sa.Column("customer_mobile", sa.String(32), nullable=True),
        sa.Column("mobile_masked", sa.String(32), nullable=True),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=True),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("body_preview", sa.Text, nullable=True),
        sa.Column("outcome_notes", sa.Text, nullable=True),
        sa.Column("disposition", CALL_DISPOSITION, nullable=True),
        sa.Column("provider_message_id", sa.String(160), nullable=True),
        sa.Column("provider_name", sa.String(64), nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.Column("delivered_at", TZ, nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("extra_json", JSONB, nullable=True),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_communication_events_work_item_id_work_items",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["ops_users.id"],
            name="fk_communication_events_actor_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_communication_events"),
    )
    op.create_index(
        "ix_communication_events_created_at", "communication_events", ["created_at"]
    )
    op.create_index("ix_communication_events_tenant_id", "communication_events", ["tenant_id"])
    op.create_index(
        "ix_communication_events_tenant_occurred",
        "communication_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_communication_events_tenant_work_item",
        "communication_events",
        ["tenant_id", "work_item_id"],
    )
    op.create_index(
        "ix_communication_events_tenant_channel_status",
        "communication_events",
        ["tenant_id", "channel", "status"],
    )
    op.create_index(
        "ix_communication_events_tenant_customer_ref",
        "communication_events",
        ["tenant_id", "customer_reference"],
    )
    op.create_index(
        "ix_communication_events_tenant_actor",
        "communication_events",
        ["tenant_id", "actor_user_id"],
    )

    # ----------------------------------------------------------------- audit
    # Append only. There is no update or delete path anywhere in the codebase.
    op.create_table(
        "ops_audit_log",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=False),
        sa.Column("action", AUDIT_ACTION, nullable=False),
        sa.Column("entity_type", AUDIT_ENTITY_TYPE, nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("before_json", JSONB, nullable=True),
        sa.Column("after_json", JSONB, nullable=True),
        sa.Column("changed_fields", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column(
            "is_system_action", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["ops_users.id"],
            name="fk_ops_audit_log_actor_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ops_audit_log"),
    )
    op.create_index("ix_ops_audit_log_created_at", "ops_audit_log", ["created_at"])
    op.create_index("ix_ops_audit_log_tenant_id", "ops_audit_log", ["tenant_id"])
    op.create_index(
        "ix_ops_audit_log_tenant_created", "ops_audit_log", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_ops_audit_log_tenant_action", "ops_audit_log", ["tenant_id", "action", "created_at"]
    )
    op.create_index(
        "ix_ops_audit_log_tenant_actor", "ops_audit_log", ["tenant_id", "actor_user_id", "created_at"]
    )
    op.create_index(
        "ix_ops_audit_log_tenant_entity", "ops_audit_log", ["tenant_id", "entity_type", "entity_id"]
    )
    op.create_index(
        "ix_ops_audit_log_tenant_trace", "ops_audit_log", ["tenant_id", "trace_id"]
    )

    # ---------------------------------------------------------- integrations
    op.create_table(
        "integration_health",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("status", INTEGRATION_HEALTH_STATUS, nullable=False),
        sa.Column("last_success_at", TZ, nullable=True),
        sa.Column("last_failure_at", TZ, nullable=True),
        sa.Column("last_sync_at", TZ, nullable=True),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("error_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_processed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error_summary", sa.Text, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("details_json", JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_integration_health"),
        sa.UniqueConstraint(
            "tenant_id", "source_app", name="uq_integration_health_tenant_source"
        ),
    )
    op.create_index("ix_integration_health_created_at", "integration_health", ["created_at"])
    op.create_index("ix_integration_health_tenant_id", "integration_health", ["tenant_id"])
    op.create_index(
        "ix_integration_health_tenant_source", "integration_health", ["tenant_id", "source_app"]
    )

    op.create_table(
        "case_sync_runs",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_app", SOURCE_APP, nullable=False),
        sa.Column("status", SYNC_RUN_STATUS, nullable=False),
        sa.Column("job_name", sa.String(120), nullable=False),
        sa.Column("queue_code", sa.String(64), nullable=True),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("completed_at", TZ, nullable=True),
        sa.Column("records_fetched", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_updated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text, nullable=True),
        sa.Column("error_detail_json", JSONB, nullable=True),
        sa.Column("triggered_by_user_id", UUID, nullable=True),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["triggered_by_user_id"],
            ["ops_users.id"],
            name="fk_case_sync_runs_triggered_by_user_id_ops_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_case_sync_runs"),
    )
    op.create_index("ix_case_sync_runs_created_at", "case_sync_runs", ["created_at"])
    op.create_index("ix_case_sync_runs_tenant_id", "case_sync_runs", ["tenant_id"])
    op.create_index(
        "ix_case_sync_runs_tenant_source_started",
        "case_sync_runs",
        ["tenant_id", "source_app", "started_at"],
    )

    op.create_table(
        "notifications",
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recipient_user_id", UUID, nullable=False),
        sa.Column("notification_type", NOTIFICATION_TYPE, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.String(600), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("link_path", sa.String(500), nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("read_at", TZ, nullable=True),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["ops_users.id"],
            name="fk_notifications_recipient_user_id_ops_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index(
        "ix_notifications_tenant_created", "notifications", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_notifications_tenant_recipient_unread",
        "notifications",
        ["tenant_id", "recipient_user_id", "is_read"],
    )


def downgrade() -> None:
    for table in (
        "notifications",
        "case_sync_runs",
        "integration_health",
        "ops_audit_log",
        "communication_events",
        "case_comments",
        "work_item_events",
        "work_items",
        "user_availability",
        "queue_sla_policies",
        "queue_memberships",
        "ops_queues",
        "sla_policies",
        "department_memberships",
        "ops_departments",
        "user_roles",
        "ops_users",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for name, values in reversed(ENUM_TYPES):
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
