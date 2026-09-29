"""Dashboard and statistics service (PRD 9, Release 2 data).

Every aggregate is computed in SQL against the tenant scope. Nothing is
pre-computed into a cache, so a number on the dashboard can always be traced
back to a row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.models.availability import (
    AvailabilityRequestStatus,
    AvailabilityType,
    UserAvailability,
)
from app.models.integration import IntegrationHealth, IntegrationHealthStatus
from app.models.org import Queue, QueueMembership
from app.models.sla import SlaStatus
from app.models.user import OperationalStatus, User
from app.models.work import OPEN_STATUSES, WorkItem, WorkStatus
from app.schemas.work import (
    ActionAlert,
    AgeingBucket,
    AgeingResponse,
    IntegrationHealthItem,
    SlaStats,
    StatusCount,
    SummaryStats,
    UserWorkloadRow,
)
from app.services import sla as sla_service
from app.services.availability import pending_approval_count


def utcnow() -> datetime:
    return datetime.now(UTC)


def counts_by(
    session: Session, statement: Select[Any], *, skip_null: bool = True
) -> dict[Any, int]:
    """Collapse a ``GROUP BY`` aggregate result into a plain count map.

    Rows are unpacked explicitly rather than handed to ``dict()`` so that a
    nullable grouping key is narrowed away instead of leaking a ``None`` key
    into the map. Callers read with ``.get(key, 0)``.
    """
    result: dict[Any, int] = {}
    for key, total in session.execute(statement):
        if key is None and skip_null:
            continue
        result[key] = int(total)
    return result


def summary(session: Session, *, tenant_id: str) -> SummaryStats:
    now = utcnow()
    end_of_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

    counts = counts_by(
        session,
        select(WorkItem.status, func.count(WorkItem.id))
        .where(WorkItem.tenant_id == tenant_id)
        .group_by(WorkItem.status),
    )

    unassigned = (
        session.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.status.in_(tuple(OPEN_STATUSES)),
                WorkItem.assigned_user_id.is_(None),
            )
        )
        or 0
    )
    locked = (
        session.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.locked_by_user_id.isnot(None),
                WorkItem.locked_until > now,
            )
        )
        or 0
    )
    breached = (
        session.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.sla_status == SlaStatus.BREACHED,
                WorkItem.status.in_(tuple(OPEN_STATUSES)),
            )
        )
        or 0
    )
    due_today = (
        session.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.due_at.isnot(None),
                WorkItem.due_at >= now,
                WorkItem.due_at < end_of_day,
                WorkItem.status.in_(tuple(OPEN_STATUSES)),
            )
        )
        or 0
    )
    unavailable_staff = (
        session.scalar(
            select(func.count(func.distinct(UserAvailability.user_id))).where(
                UserAvailability.tenant_id == tenant_id,
                UserAvailability.status == AvailabilityRequestStatus.APPROVED,
                UserAvailability.availability_type != AvailabilityType.BREAK,
                UserAvailability.start_at <= now,
                UserAvailability.end_at > now,
            )
        )
        or 0
    )
    active_members = (
        session.scalar(
            select(func.count(func.distinct(QueueMembership.user_id))).where(
                QueueMembership.tenant_id == tenant_id, QueueMembership.is_active.is_(True)
            )
        )
        or 0
    )
    open_queues = (
        session.scalar(
            select(func.count(Queue.id)).where(
                Queue.tenant_id == tenant_id, Queue.status == "ACTIVE"
            )
        )
        or 0
    )

    return SummaryStats(
        open_work=sum(count for status, count in counts.items() if status in OPEN_STATUSES),
        assigned_work=counts.get(WorkStatus.ASSIGNED, 0) + counts.get(WorkStatus.IN_PROGRESS, 0),
        in_progress_work=counts.get(WorkStatus.IN_PROGRESS, 0),
        unassigned_work=unassigned,
        locked_cases=locked,
        sla_breaches=breached,
        due_today=due_today,
        unavailable_staff=unavailable_staff,
        active_queue_members=active_members,
        pending_approvals=pending_approval_count(session, tenant_id=tenant_id),
        open_queues=open_queues,
        generated_at=now,
    )


def work_by_status(session: Session, *, tenant_id: str) -> list[StatusCount]:
    rows = session.execute(
        select(WorkItem.status, func.count(WorkItem.id))
        .where(WorkItem.tenant_id == tenant_id)
        .group_by(WorkItem.status)
    ).all()
    return [
        StatusCount(status=status.value, count=count)
        for status, count in sorted(rows, key=lambda item: item[0].value)
    ]


def work_by_department(session: Session, *, tenant_id: str) -> list[StatusCount]:
    # One expression object, referenced by both the projection and the GROUP BY.
    # Two separate func.coalesce(...) calls render the literal as two distinct
    # bind parameters, so Postgres sees SELECT coalesce(dept, $1) but
    # GROUP BY coalesce(dept, $2) and rejects the whole query with
    # "column must appear in the GROUP BY clause". Sharing the object makes
    # SQLAlchemy bind it once.
    department = func.coalesce(WorkItem.department_code, "UNASSIGNED")
    rows = session.execute(
        select(department, func.count(WorkItem.id))
        .where(WorkItem.tenant_id == tenant_id, WorkItem.status.in_(tuple(OPEN_STATUSES)))
        .group_by(department)
    ).all()
    return [StatusCount(status=code, count=count) for code, count in sorted(rows)]


def queue_workload(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    """Per-queue open counts plus available/eligible member counts."""
    now = utcnow()
    queues = list(
        session.scalars(
            select(Queue)
            .where(Queue.tenant_id == tenant_id, Queue.status == "ACTIVE")
            .order_by(Queue.code)
        )
    )
    if not queues:
        return []

    queue_codes = [queue.code for queue in queues]
    queue_ids = [queue.id for queue in queues]

    open_rows = counts_by(
        session,
        select(WorkItem.queue_code, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.queue_code.in_(queue_codes),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        )
        .group_by(WorkItem.queue_code),
    )
    unassigned_rows = counts_by(
        session,
        select(WorkItem.queue_code, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.queue_code.in_(queue_codes),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
            WorkItem.assigned_user_id.is_(None),
        )
        .group_by(WorkItem.queue_code),
    )
    overdue_rows = counts_by(
        session,
        select(WorkItem.queue_code, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.queue_code.in_(queue_codes),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
            WorkItem.due_at.isnot(None),
            WorkItem.due_at < now,
        )
        .group_by(WorkItem.queue_code),
    )

    member_rows = counts_by(
        session,
        select(QueueMembership.queue_id, func.count(QueueMembership.id))
        .where(
            QueueMembership.tenant_id == tenant_id,
            QueueMembership.queue_id.in_(queue_ids),
            QueueMembership.is_active.is_(True),
        )
        .group_by(QueueMembership.queue_id),
    )

    available_rows = counts_by(
        session,
        select(QueueMembership.queue_id, func.count(func.distinct(QueueMembership.user_id)))
        .join(User, User.id == QueueMembership.user_id)
        .where(
            QueueMembership.tenant_id == tenant_id,
            QueueMembership.queue_id.in_(queue_ids),
            QueueMembership.is_active.is_(True),
            User.status == OperationalStatus.ACTIVE,
            User.is_available.is_(True),
        )
        .group_by(QueueMembership.queue_id),
    )

    results: list[dict[str, Any]] = []
    for queue in queues:
        open_count = open_rows.get(queue.code, 0)
        members = member_rows.get(queue.id, 0)
        available = available_rows.get(queue.id, 0)
        capacity = members * queue.max_workload_per_user
        results.append(
            {
                "queue_code": queue.code,
                "queue_name": queue.name,
                "source_app": queue.source_app,
                "open_count": open_count,
                "unassigned_count": unassigned_rows.get(queue.code, 0),
                "assigned_count": open_count - unassigned_rows.get(queue.code, 0),
                "overdue_count": overdue_rows.get(queue.code, 0),
                "eligible_members": members,
                "available_members": available,
                "capacity_utilisation_pct": round((open_count / capacity) * 100, 1)
                if capacity
                else 0.0,
            }
        )
    return results


def _counts_by_user(
    session: Session,
    *,
    tenant_id: str,
    user_ids: Sequence[uuid.UUID],
    column: InstrumentedAttribute[uuid.UUID | None],
    extra: ColumnElement[bool] | tuple[ColumnElement[bool], ...],
) -> dict[uuid.UUID, int]:
    """Group a per-user count in SQL.

    ``Row`` objects are unpacked explicitly rather than fed to ``dict()`` so the
    optional key column is narrowed and absent keys are simply missing from the
    result - callers use ``.get(user_id, 0)``.
    """
    conditions = extra if isinstance(extra, tuple) else (extra,)
    raw: dict[Any, int] = counts_by(
        session,
        select(column, func.count(WorkItem.id))
        .where(WorkItem.tenant_id == tenant_id, column.in_(user_ids), *conditions)
        .group_by(column),
    )
    return {key: value for key, value in raw.items() if isinstance(key, uuid.UUID)}


def user_workload(session: Session, *, tenant_id: str, limit: int = 20) -> list[UserWorkloadRow]:
    now = utcnow()
    users = list(
        session.scalars(
            select(User)
            .where(User.tenant_id == tenant_id, User.status == OperationalStatus.ACTIVE)
            .order_by(User.display_name)
            .limit(limit)
        )
    )
    if not users:
        return []

    user_ids = [user.id for user in users]
    open_rows = _counts_by_user(
        session,
        tenant_id=tenant_id,
        user_ids=user_ids,
        column=WorkItem.assigned_user_id,
        extra=WorkItem.status.in_(tuple(OPEN_STATUSES)),
    )
    locked_rows = _counts_by_user(
        session,
        tenant_id=tenant_id,
        user_ids=user_ids,
        column=WorkItem.locked_by_user_id,
        extra=WorkItem.locked_until > now,
    )
    breached_rows = _counts_by_user(
        session,
        tenant_id=tenant_id,
        user_ids=user_ids,
        column=WorkItem.assigned_user_id,
        extra=(
            WorkItem.sla_status == SlaStatus.BREACHED,
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        ),
    )

    from app.services.users import department_codes_by_user

    dept_rows = department_codes_by_user(session, tenant_id=tenant_id, user_ids=user_ids)

    rows: list[UserWorkloadRow] = []
    for user in users:
        workload = open_rows.get(user.id, 0)
        rows.append(
            UserWorkloadRow(
                user_id=user.id,
                display_name=user.display_name,
                username=user.username,
                is_available=user.is_available,
                open_workload=workload,
                capacity=user.capacity,
                utilisation_pct=round((workload / user.capacity) * 100, 1)
                if user.capacity
                else 0.0,
                locked_cases=locked_rows.get(user.id, 0),
                breached_cases=breached_rows.get(user.id, 0),
                department_codes=sorted(dept_rows.get(user.id, [])),
            )
        )
    rows.sort(key=lambda item: (-item.open_workload, item.display_name.casefold()))
    return rows


def ageing(session: Session, *, tenant_id: str) -> AgeingResponse:
    now = utcnow()
    rows = session.execute(
        select(WorkItem.created_at).where(
            WorkItem.tenant_id == tenant_id, WorkItem.status.in_(tuple(OPEN_STATUSES))
        )
    ).scalars()
    counts: dict[str, int] = {label: 0 for label, _, _ in sla_service.AGEING_BUCKETS}
    for created_at in rows:
        hours = (now - created_at).total_seconds() / 3600
        counts[sla_service.bucket_for_age(hours)] += 1
    buckets = [
        AgeingBucket(label=label, min_hours=minimum, max_hours=maximum, count=counts[label])
        for label, minimum, maximum in sla_service.AGEING_BUCKETS
    ]
    return AgeingResponse(buckets=buckets, total=sum(counts.values()), generated_at=now)


def sla_stats(session: Session, *, tenant_id: str) -> SlaStats:
    rows = session.execute(
        select(WorkItem.sla_status, func.count(WorkItem.id))
        .where(WorkItem.tenant_id == tenant_id)
        .group_by(WorkItem.sla_status)
    ).all()
    counts = {status.value: count for status, count in rows}
    open_total = sum(count for status, count in rows if status != SlaStatus.COMPLETED)
    breached = counts.get(SlaStatus.BREACHED.value, 0)
    return SlaStats(
        not_started=counts.get(SlaStatus.NOT_STARTED.value, 0),
        on_track=counts.get(SlaStatus.ON_TRACK.value, 0),
        due_soon=counts.get(SlaStatus.DUE_SOON.value, 0),
        breached=breached,
        completed=counts.get(SlaStatus.COMPLETED.value, 0),
        total_open=open_total,
        breach_rate_pct=round((breached / open_total) * 100, 1) if open_total else 0.0,
        generated_at=utcnow(),
    )


def integration_health(session: Session, *, tenant_id: str) -> list[IntegrationHealthItem]:
    """Derived from stored probe results - never hardcoded (PRD 25)."""
    rows = list(
        session.scalars(
            select(IntegrationHealth)
            .where(IntegrationHealth.tenant_id == tenant_id)
            .order_by(IntegrationHealth.source_app)
        )
    )
    return [
        IntegrationHealthItem(
            source_app=row.source_app,
            status=row.status.value,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            last_sync_at=row.last_sync_at,
            latency_ms=row.latency_ms,
            error_count=row.error_count,
            records_processed=row.records_processed,
            records_failed=row.records_failed,
            consecutive_failures=row.consecutive_failures,
            last_error_summary=row.last_error_summary,
        )
        for row in rows
    ]


def build_alerts(
    *,
    summary_stats: SummaryStats,
    queue_rows: Sequence[dict[str, Any]],
    workload_rows: Sequence[UserWorkloadRow],
    health: Sequence[IntegrationHealthItem],
    lock_expiring_soon: int = 0,
) -> list[ActionAlert]:
    """Actionable alerts, each linking to the filtered screen that resolves it."""
    alerts: list[ActionAlert] = []

    if summary_stats.sla_breaches:
        alerts.append(
            ActionAlert(
                key="sla-breaches",
                severity="CRITICAL",
                title=f"{summary_stats.sla_breaches} cases have breached SLA",
                detail="Open work whose due time has passed.",
                count=summary_stats.sla_breaches,
                link_path="/work",
                filter_params={"sla_state": "BREACHED"},
            )
        )
    if summary_stats.due_today:
        alerts.append(
            ActionAlert(
                key="due-today",
                severity="WARNING",
                title=f"{summary_stats.due_today} cases are due today",
                detail="Work with a deadline later today.",
                count=summary_stats.due_today,
                link_path="/work",
                filter_params={"due_today": "true"},
            )
        )

    starved = [row for row in queue_rows if row["eligible_members"] == 0]
    if starved:
        alerts.append(
            ActionAlert(
                key="queues-without-members",
                severity="CRITICAL",
                title=f"{len(starved)} queues have no active members",
                detail=", ".join(row["queue_code"] for row in starved[:5]),
                count=len(starved),
                link_path="/queues",
                filter_params={},
            )
        )
    no_available = [
        row for row in queue_rows if row["eligible_members"] > 0 and row["available_members"] == 0
    ]
    if no_available:
        alerts.append(
            ActionAlert(
                key="queues-without-available-agents",
                severity="CRITICAL",
                title=f"{len(no_available)} queues have no available agents",
                detail=", ".join(row["queue_code"] for row in no_available[:5]),
                count=len(no_available),
                link_path="/queues",
                filter_params={},
            )
        )

    over_capacity = [row for row in queue_rows if row["capacity_utilisation_pct"] >= 90]
    if over_capacity:
        alerts.append(
            ActionAlert(
                key="queue-capacity",
                severity="WARNING",
                title=f"{len(over_capacity)} queues are above 90% capacity",
                detail=", ".join(row["queue_code"] for row in over_capacity[:5]),
                count=len(over_capacity),
                link_path="/queues",
                filter_params={},
            )
        )

    if summary_stats.pending_approvals:
        alerts.append(
            ActionAlert(
                key="pending-approvals",
                severity="INFO",
                title=f"{summary_stats.pending_approvals} availability requests await approval",
                detail="Requests need a decision from somebody other than the requester.",
                count=summary_stats.pending_approvals,
                link_path="/availability",
                filter_params={"status": "REQUESTED"},
            )
        )

    if lock_expiring_soon:
        alerts.append(
            ActionAlert(
                key="locks-expiring",
                severity="INFO",
                title=f"{lock_expiring_soon} locks are expiring within 10 minutes",
                detail="Send a heartbeat or release the case.",
                count=lock_expiring_soon,
                link_path="/work",
                filter_params={"locked": "true"},
            )
        )

    stale_minutes = 8
    for item in health:
        if item.status == IntegrationHealthStatus.ISOLATED:
            continue
        if item.status in (IntegrationHealthStatus.FAILED, IntegrationHealthStatus.DEGRADED):
            alerts.append(
                ActionAlert(
                    key=f"integration-{item.source_app.value.lower()}-unhealthy",
                    severity="CRITICAL",
                    title=f"{item.source_app.value} is {item.status.lower()}",
                    detail=item.last_error_summary or "No successful sync recorded recently.",
                    count=item.error_count,
                    link_path="/admin/integration-health",
                    filter_params={"source_app": item.source_app.value},
                )
            )
        elif item.last_sync_at and (utcnow() - item.last_sync_at) > timedelta(
            minutes=stale_minutes
        ):
            minutes = int((utcnow() - item.last_sync_at).total_seconds() // 60)
            alerts.append(
                ActionAlert(
                    key=f"integration-{item.source_app.value.lower()}-stale",
                    severity="WARNING",
                    title=f"{item.source_app.value} sync has not completed for {minutes} minutes",
                    detail="Ops remains usable; upstream work may be delayed.",
                    count=minutes,
                    link_path="/admin/integration-health",
                    filter_params={"source_app": item.source_app.value},
                )
            )
    return alerts


def locks_expiring_within(session: Session, *, tenant_id: str, minutes: int = 10) -> int:
    now = utcnow()
    return (
        session.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.locked_until.isnot(None),
                WorkItem.locked_until > now,
                WorkItem.locked_until <= now + timedelta(minutes=minutes),
            )
        )
        or 0
    )


__all__ = [
    "ageing",
    "build_alerts",
    "integration_health",
    "locks_expiring_within",
    "queue_workload",
    "sla_stats",
    "summary",
    "user_workload",
    "work_by_department",
    "work_by_status",
]
