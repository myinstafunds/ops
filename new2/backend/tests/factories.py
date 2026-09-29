"""Row builders for database-backed tests.

Deliberately dumb: each function inserts one row and returns it. Fixtures
compose them so a test reads as the scenario it is checking rather than as a
pile of column defaults.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.org import Department, Queue, SourceApp
from app.models.sla import SlaPolicy
from app.models.user import OperationalStatus, User, UserRole
from app.models.work import Priority, WorkItem, WorkStatus

TENANT = "BPSEC"


def make_user(
    session: Session,
    *,
    tenant_id: str = TENANT,
    username: str | None = None,
    display_name: str | None = None,
    roles: tuple[str, ...] = (),
    status: OperationalStatus = OperationalStatus.ACTIVE,
    is_available: bool = True,
    capacity: int = 25,
    priority_weight: int = 0,
    daily_allocation_limit: int = 50,
    specialisations: str | None = None,
) -> User:
    handle = username or f"user-{uuid.uuid4().hex[:8]}"
    user = User(
        tenant_id=tenant_id,
        username=handle,
        display_name=display_name or handle.title(),
        bre_user_id=f"bre-{uuid.uuid4().hex[:12]}",
        email=f"{handle}@bps.com",
        status=status,
        is_available=is_available,
        capacity=capacity,
        priority_weight=priority_weight,
        daily_allocation_limit=daily_allocation_limit,
        specialisations=specialisations,
    )
    session.add(user)
    session.flush()
    for role in roles:
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role))
    session.flush()
    session.refresh(user)
    return user


def make_department(
    session: Session,
    *,
    tenant_id: str = TENANT,
    code: str | None = None,
    name: str = "Test Department",
) -> Department:
    department = Department(
        tenant_id=tenant_id,
        code=code or f"DEPT{uuid.uuid4().hex[:6].upper()}",
        name=name,
        source_app=SourceApp.OPS,
        is_active=True,
    )
    session.add(department)
    session.flush()
    return department


def make_queue(
    session: Session,
    *,
    tenant_id: str = TENANT,
    code: str | None = None,
    name: str = "Test Queue",
    department_id: uuid.UUID | None = None,
    source_app: SourceApp = SourceApp.LOS,
    is_active: bool = True,
) -> Queue:
    queue = Queue(
        tenant_id=tenant_id,
        code=code or f"Q{uuid.uuid4().hex[:6].upper()}",
        name=name,
        department_id=department_id,
        source_app=source_app,
        is_active=is_active,
    )
    session.add(queue)
    session.flush()
    return queue


def make_sla_policy(
    session: Session,
    *,
    tenant_id: str = TENANT,
    code: str | None = None,
    target_minutes: int = 240,
    warning_minutes: int = 60,
) -> SlaPolicy:
    policy = SlaPolicy(
        tenant_id=tenant_id,
        code=code or f"SLA{uuid.uuid4().hex[:6].upper()}",
        name="Test policy",
        source_app=SourceApp.LOS,
        target_minutes=target_minutes,
        warning_minutes=warning_minutes,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    return policy


def make_work_item(
    session: Session,
    *,
    tenant_id: str = TENANT,
    queue_code: str = "LOS",
    department_code: str | None = "LOANS",
    status: WorkStatus = WorkStatus.OPEN,
    priority: Priority = Priority.MEDIUM,
    title: str = "Card not reflecting credit limit",
    assigned_user_id: uuid.UUID | None = None,
    assigned_user_name: str | None = None,
    due_at: datetime | None = None,
    source_app: SourceApp = SourceApp.LOS,
    source_key: str | None = None,
    external_ref: str | None = None,
    customer_name: str | None = "Rahul Kumar",
    customer_mobile: str | None = "919876543210",
) -> WorkItem:
    item = WorkItem(
        tenant_id=tenant_id,
        source_app=source_app,
        source_key=source_key if source_key is not None else f"LOS:{uuid.uuid4().hex[:10]}",
        external_ref=external_ref,
        title=title,
        customer_name=customer_name,
        customer_mobile=customer_mobile,
        queue_code=queue_code,
        department_code=department_code,
        status=status,
        priority=priority,
        assigned_user_id=assigned_user_id,
        assigned_user_name=assigned_user_name,
        due_at=due_at,
    )
    session.add(item)
    session.flush()
    return item


def audit_rows(session: Session, *, tenant_id: str = TENANT) -> list[AuditLog]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def hours_from_now(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


def as_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
