"""SLA computation (PRD 22).

``due_at`` comes from the queue's SLA policy unless an authorised user
overrode it. The state machine is deliberately simple and deterministic so it
can be unit tested without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models.sla import SlaPolicy, SlaStatus
from app.models.work import WorkItem, WorkStatus


@dataclass(frozen=True, slots=True)
class SlaResolution:
    due_at: datetime | None
    status: SlaStatus
    target_minutes: int | None = None
    warning_minutes: int | None = None
    breached_at: datetime | None = None


def compute_due_at(*, created_at: datetime, policy: SlaPolicy | None) -> datetime | None:
    """Deadline for a new work item under the supplied policy."""
    if policy is None:
        return None
    return created_at + timedelta(minutes=policy.target_minutes)


def compute_status(
    *,
    due_at: datetime | None,
    status: WorkStatus,
    started_at: datetime | None,
    closed_at: datetime | None,
    breached_at: datetime | None,
    now: datetime | None = None,
    warning_minutes: int | None = None,
) -> SlaStatus:
    """Classify a work item against its deadline.

    Precedence: closed items are COMPLETED, then an already-recorded breach
    stays BREACHED, then deadline comparison.
    """
    moment = now or datetime.now(UTC)

    if status in (WorkStatus.CLOSED, WorkStatus.CANCELLED):
        return SlaStatus.COMPLETED
    if breached_at is not None:
        return SlaStatus.BREACHED
    if due_at is None:
        return SlaStatus.NOT_STARTED if started_at is None else SlaStatus.ON_TRACK

    if due_at <= moment:
        return SlaStatus.BREACHED
    warning = warning_minutes if warning_minutes is not None else settings.ops_sla_due_soon_minutes
    if due_at - moment <= timedelta(minutes=warning):
        return SlaStatus.DUE_SOON
    return SlaStatus.ON_TRACK


def resolve_for_item(
    item: WorkItem, *, policy: SlaPolicy | None = None, now: datetime | None = None
) -> SlaResolution:
    """Recompute the SLA fields for an existing work item."""
    moment = now or datetime.now(UTC)
    target = policy.target_minutes if policy else None
    warning = policy.warning_minutes if policy else None

    status = compute_status(
        due_at=item.due_at,
        status=item.status,
        started_at=item.assigned_at,
        closed_at=item.closed_at,
        breached_at=item.sla_breached_at,
        now=moment,
        warning_minutes=warning,
    )
    breached_at = item.sla_breached_at
    if status is SlaStatus.BREACHED and breached_at is None:
        breached_at = item.due_at or moment
    return SlaResolution(
        due_at=item.due_at,
        status=status,
        target_minutes=target,
        warning_minutes=warning,
        breached_at=breached_at,
    )


def mark_breaches(items: list[WorkItem], *, now: datetime | None = None) -> list[WorkItem]:
    """Return the subset of ``items`` that have newly breached.

    Used by the SLA sweep worker (Release 2) and by the dashboard so the
    ``BREACHED`` state is derived, never stored stale.
    """
    moment = now or datetime.now(UTC)
    breached: list[WorkItem] = []
    for item in items:
        if item.sla_breached_at is not None:
            continue
        if item.status in (WorkStatus.CLOSED, WorkStatus.CANCELLED):
            continue
        if item.due_at is not None and item.due_at <= moment:
            breached.append(item)
    return breached


def seconds_remaining(due_at: datetime | None, *, now: datetime | None = None) -> int | None:
    if due_at is None:
        return None
    moment = now or datetime.now(UTC)
    return int((due_at - moment).total_seconds())


AGEING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("< 4 hours", 0, 4),
    ("4 - 24 hours", 4, 24),
    ("1 - 3 days", 24, 72),
    ("3 - 7 days", 72, 168),
    ("7 - 30 days", 168, 720),
    ("> 30 days", 720, None),
)


def bucket_for_age(hours: float) -> str:
    for label, minimum, maximum in AGEING_BUCKETS:
        if hours >= minimum and (maximum is None or hours < maximum):
            return label
    return AGEING_BUCKETS[-1][0]
