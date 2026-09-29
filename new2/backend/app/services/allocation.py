"""Auto allocation engine (PRD 17).

Design notes:

* Eligibility is a pure function of data already fetched for the queue, so the
  decision is explainable and testable without touching the network.
* Selection order is fixed: lowest workload, then highest priority weight,
  then specialisation match, then display name. The order is deterministic,
  which means two managers looking at the same queue see the same answer.
* Every decision returns the factors that produced it, so the UI can explain
  itself (PRD 17.2).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dt_time

from app.models.org import Queue, QueueMembership
from app.models.user import User
from app.models.work import OPEN_STATUSES

#: Reasons a candidate is rejected, in the order they are evaluated.
REASON_QUEUE_INACTIVE = "queue membership is inactive"
REASON_USER_INACTIVE = "user is not an active operational user"
REASON_UNAVAILABLE = "an approved unavailable window is in force"
REASON_AT_CAPACITY = "already at capacity"
REASON_PERMISSION = "does not hold the required permission"
REASON_DEPARTMENT = "is not a member of the required department"
REASON_ROLE = "does not hold an eligible role for this queue"
REASON_SPECIALISATION = "does not match the required specialisation"
REASON_SHIFT_ENDED = "the scheduled shift has not started or has ended"
REASON_DAILY_LIMIT = "the daily allocation limit has been reached"


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """Everything the algorithm needs about one queue member."""

    user_id: uuid.UUID
    display_name: str
    username: str
    membership: QueueMembership
    user: User
    department_ids: frozenset[uuid.UUID] = frozenset()
    open_workload: int = 0
    daily_allocations_today: int = 0
    unavailable_until: datetime | None = None


@dataclass(slots=True)
class CandidateResult:
    candidate: CandidateInput
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    specialisation_match: bool = False
    effective_capacity: int = 0

    @property
    def sort_key(self) -> tuple[int, int, int, str, str]:
        """PRD 17.1 selection order, encoded once.

        (workload, -priority_weight, -specialisation_match, display_name,
        username) - the username tiebreaker removes any possibility of a
        non-deterministic pick between two identical staff.
        """
        return (
            self.candidate.open_workload,
            -self.candidate.membership.priority_weight,
            0 if self.specialisation_match else 1,
            self.candidate.display_name.casefold(),
            self.candidate.username.casefold(),
        )


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    chosen: CandidateResult
    considered: int
    eligible_count: int
    factors: tuple[str, ...]
    rejected_reasons: tuple[str, ...]

    def explanation(self) -> str:
        header = f"Assigned to {self.chosen.candidate.display_name} because:"
        bullets = "\n".join(f"- {factor}" for factor in self.factors)
        return f"{header}\n{bullets}"


def _shift_covers(membership: QueueMembership, moment: datetime) -> bool:
    """True when ``moment`` sits inside the member's scheduled shift.

    A member with no shift configured is always in shift.
    """
    if membership.shift_start is None or membership.shift_end is None:
        return True
    shift_days = membership.shift_days or []
    # Python: Monday == 0. Stored as 0..6 to match.
    if shift_days and moment.weekday() not in shift_days:
        return False
    local_time = moment.timetz().replace(tzinfo=None)
    start = dt_time(
        membership.shift_start.hour, membership.shift_start.minute, membership.shift_start.second
    )
    end = dt_time(
        membership.shift_end.hour, membership.shift_end.minute, membership.shift_end.second
    )
    if start <= end:
        return start <= local_time <= end
    # Overnight shift, e.g. 22:00 to 06:00.
    return local_time >= start or local_time <= end


def evaluate_candidate(
    candidate: CandidateInput,
    *,
    queue: Queue,
    required_permission: str | None,
    now: datetime,
) -> CandidateResult:
    result = CandidateResult(candidate=candidate, eligible=True)
    user = candidate.user
    membership = candidate.membership

    #: Per-queue capacity wins, otherwise the queue default.
    result.effective_capacity = (
        membership.capacity if membership.capacity is not None else queue.max_workload_per_user
    )

    if not membership.is_active:
        _reject(result, REASON_QUEUE_INACTIVE)
    if user.status.value != "ACTIVE":
        _reject(result, REASON_USER_INACTIVE)
    if not user.is_available or candidate.unavailable_until is not None:
        _reject(result, REASON_UNAVAILABLE)
    if candidate.open_workload >= result.effective_capacity:
        _reject(result, REASON_AT_CAPACITY)

    if required_permission and required_permission not in _permissions_for(user):
        _reject(result, REASON_PERMISSION)

    if queue.department_id is not None and queue.department_id not in candidate.department_ids:
        _reject(result, REASON_DEPARTMENT)

    eligible_roles = queue.eligible_roles or []
    if eligible_roles:
        user_roles = {role.role for role in user.roles}
        if not user_roles.intersection(eligible_roles):
            _reject(result, REASON_ROLE)

    required_specialisations = {item.casefold() for item in (queue.required_specialisations or [])}
    if required_specialisations:
        user_specialisations = {item.casefold() for item in user.specialisation_list}
        membership_specialisation = (membership.specialisation or "").casefold()
        result.specialisation_match = bool(
            user_specialisations.intersection(required_specialisations)
            or (membership_specialisation and membership_specialisation in required_specialisations)
        )
        if not result.specialisation_match:
            _reject(result, REASON_SPECIALISATION)

    daily_limit = (
        membership.daily_max_allocations
        if membership.daily_max_allocations is not None
        else user.daily_allocation_limit
    )
    if daily_limit is not None and candidate.daily_allocations_today >= daily_limit:
        _reject(result, REASON_DAILY_LIMIT)

    if not _shift_covers(membership, now):
        _reject(result, REASON_SHIFT_ENDED)

    result.eligible = not result.reasons
    return result


def select(candidates: Sequence[CandidateResult]) -> CandidateResult | None:
    eligible = [item for item in candidates if item.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda item: item.sort_key)


def build_factors(
    chosen: CandidateResult, queue: Queue, *, lowest_workload: int
) -> tuple[str, ...]:
    """Human readable justification for the chosen candidate (PRD 17.2)."""
    user = chosen.candidate.user
    membership = chosen.candidate.membership
    factors: list[str] = [
        f"queue membership active in {queue.code}",
        "user is available (no approved unavailable window in force)",
        f"workload {chosen.candidate.open_workload}/{chosen.effective_capacity} is within capacity",
    ]

    daily_limit = (
        membership.daily_max_allocations
        if membership.daily_max_allocations is not None
        else user.daily_allocation_limit
    )
    factors.append(f"daily allocations {chosen.candidate.daily_allocations_today}/{daily_limit}")

    if membership.priority_weight:
        factors.append(f"queue priority weight {membership.priority_weight}")
    if chosen.specialisation_match:
        factors.append("specialisation matches the queue requirement")
    factors.append(f"selected with the lowest eligible workload ({lowest_workload})")
    return tuple(factors)


def rejection_summary(results: Sequence[CandidateResult]) -> list[str]:
    """Aggregate why nobody was eligible, most common reason first (PRD 17.3)."""
    counts: dict[str, int] = {}
    for result in results:
        for reason in result.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return ["no eligible queue members"]
    return [
        f"{reason} ({count} member{'s' if count != 1 else ''})"
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _reject(result: CandidateResult, reason: str) -> None:
    if reason not in result.reasons:
        result.reasons.append(reason)


def _permissions_for(user: User) -> frozenset[str]:
    from app.security.permissions import permissions_for_roles

    return permissions_for_roles([role.role for role in user.roles])


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "OPEN_STATUSES",
    "AllocationDecision",
    "CandidateInput",
    "CandidateResult",
    "build_factors",
    "evaluate_candidate",
    "rejection_summary",
    "select",
    "utcnow",
]
