"""Allocation algorithm (PRD 17).

The engine is a pure function of pre-fetched data, so these tests build model
objects directly and need no database. What is being pinned down:

* every rejection reason is reported (a manager must be able to explain it);
* the selection order is deterministic and matches the PRD;
* a full queue produces ``None`` plus an explanation rather than a guess.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time

import pytest

from app.models.org import Queue, QueueMembership
from app.models.user import OperationalStatus, User, UserRole
from app.models.work import OPEN_STATUSES
from app.security.permissions import Permission, RoleKey
from app.services import allocation as alloc

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

QUEUE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEPARTMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def make_queue(**overrides: object) -> Queue:
    defaults: dict[str, object] = {
        "id": QUEUE_ID,
        "tenant_id": "BPSEC",
        "code": "LOS_RECOVERY",
        "name": "LOS Recovery",
        "department_id": DEPARTMENT_ID,
        "max_workload_per_user": 10,
        "eligible_roles": None,
        "required_specialisations": None,
    }
    return Queue(**{**defaults, **overrides})  # type: ignore[arg-type]


def make_user(
    *,
    roles: tuple[str, ...] = (RoleKey.AGENT,),
    status: OperationalStatus = OperationalStatus.ACTIVE,
    is_available: bool = True,
    capacity: int = 10,
    daily_limit: int = 20,
    specialisations: str | None = None,
    name: str = "Agent",
) -> User:
    user = User(
        id=uuid.uuid4(),
        tenant_id="BPSEC",
        username=name.lower(),
        display_name=name,
        status=status,
        is_available=is_available,
        capacity=capacity,
        daily_allocation_limit=daily_limit,
        specialisations=specialisations,
    )
    user.roles = [
        UserRole(id=uuid.uuid4(), tenant_id="BPSEC", user_id=user.id, role=role) for role in roles
    ]
    return user


def make_membership(
    user: User,
    *,
    queue_id: uuid.UUID = QUEUE_ID,
    is_active: bool = True,
    capacity: int | None = None,
    priority_weight: int = 0,
    daily_max: int | None = None,
    specialisation: str | None = None,
    shift_start: dt_time | None = None,
    shift_end: dt_time | None = None,
    shift_days: list[int] | None = None,
) -> QueueMembership:
    return QueueMembership(
        id=uuid.uuid4(),
        tenant_id="BPSEC",
        queue_id=queue_id,
        user_id=user.id,
        is_active=is_active,
        capacity=capacity,
        priority_weight=priority_weight,
        daily_max_allocations=daily_max,
        specialisation=specialisation,
        shift_start=shift_start,
        shift_end=shift_end,
        shift_days=shift_days,
    )


def make_candidate(
    user: User,
    membership: QueueMembership,
    **overrides: object,
) -> alloc.CandidateInput:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "display_name": user.display_name,
        "username": user.username,
        "membership": membership,
        "user": user,
        "department_ids": frozenset({DEPARTMENT_ID}),
    }
    return alloc.CandidateInput(**{**defaults, **overrides})  # type: ignore[arg-type]


def evaluate(
    candidate: alloc.CandidateInput, queue: Queue | None = None, **kwargs: object
) -> alloc.CandidateResult:
    return alloc.evaluate_candidate(
        candidate,
        queue=queue or make_queue(),
        required_permission=kwargs.get("required_permission"),  # type: ignore[arg-type]
        now=kwargs.get("now", NOW),  # type: ignore[arg-type]
    )


def test_an_available_agent_inside_capacity_is_eligible() -> None:
    user = make_user()
    result = evaluate(make_candidate(user, make_membership(user)))
    assert result.eligible
    assert result.reasons == []


def test_inactive_membership_is_rejected() -> None:
    user = make_user()
    result = evaluate(make_candidate(user, make_membership(user, is_active=False)))
    assert not result.eligible
    assert alloc.REASON_QUEUE_INACTIVE in result.reasons


def test_inactive_user_is_rejected() -> None:
    user = make_user(status=OperationalStatus.INACTIVE)
    result = evaluate(make_candidate(user, make_membership(user)))
    assert alloc.REASON_USER_INACTIVE in result.reasons


def test_approved_unavailable_window_blocks_allocation() -> None:
    user = make_user(is_available=False)
    result = evaluate(make_candidate(user, make_membership(user)))
    assert alloc.REASON_UNAVAILABLE in result.reasons


def test_workload_at_capacity_is_rejected() -> None:
    user = make_user()
    membership = make_membership(user, capacity=5)
    result = evaluate(make_candidate(user, membership, open_workload=5))
    assert alloc.REASON_AT_CAPACITY in result.reasons


def test_workload_below_capacity_is_accepted() -> None:
    user = make_user()
    membership = make_membership(user, capacity=5)
    result = evaluate(make_candidate(user, membership, open_workload=4))
    assert result.eligible


def test_missing_required_permission_is_rejected() -> None:
    user = make_user(roles=(RoleKey.AGENT,))
    result = evaluate(
        make_candidate(user, make_membership(user)),
        required_permission=Permission.OPS_CASES_CLOSE,
    )
    assert alloc.REASON_PERMISSION in result.reasons


def test_holding_the_required_permission_passes() -> None:
    user = make_user(roles=(RoleKey.OPS_MANAGER,))
    result = evaluate(
        make_candidate(user, make_membership(user)),
        required_permission=Permission.OPS_CASES_CLOSE,
    )
    assert result.eligible


def test_non_member_of_the_required_department_is_rejected() -> None:
    user = make_user()
    candidate = make_candidate(user, make_membership(user), department_ids=frozenset())
    result = evaluate(candidate)
    assert alloc.REASON_DEPARTMENT in result.reasons


def test_ineligible_role_is_rejected_when_the_queue_restricts_roles() -> None:
    user = make_user(roles=(RoleKey.AGENT,))
    queue = make_queue(eligible_roles=[RoleKey.CREDIT_SPECIALIST])
    result = evaluate(make_candidate(user, make_membership(user)), queue=queue)
    assert alloc.REASON_ROLE in result.reasons


def test_specialisation_must_match_when_required() -> None:
    user = make_user(specialisations="collections, telephony")
    queue = make_queue(required_specialisations=["underwriting"])
    result = evaluate(make_candidate(user, make_membership(user)), queue=queue)
    assert alloc.REASON_SPECIALISATION in result.reasons
    assert not result.specialisation_match


def test_specialisation_match_is_recorded_when_the_user_has_it() -> None:
    user = make_user(specialisations="underwriting")
    queue = make_queue(required_specialisations=["Underwriting"])
    result = evaluate(make_candidate(user, make_membership(user)), queue=queue)
    assert result.specialisation_match
    assert result.eligible


def test_daily_allocation_limit_is_enforced() -> None:
    user = make_user(daily_limit=3)
    result = evaluate(make_candidate(user, make_membership(user), daily_allocations_today=3))
    assert alloc.REASON_DAILY_LIMIT in result.reasons


def test_membership_daily_limit_overrides_the_user_limit() -> None:
    user = make_user(daily_limit=50)
    membership = make_membership(user, daily_max=2)
    result = evaluate(make_candidate(user, membership, daily_allocations_today=2))
    assert alloc.REASON_DAILY_LIMIT in result.reasons


def test_outside_the_scheduled_shift_is_rejected() -> None:
    user = make_user()
    membership = make_membership(
        user,
        shift_start=dt_time(14, 0),
        shift_end=dt_time(18, 0),
    )
    result = evaluate(make_candidate(user, membership), now=NOW)
    assert alloc.REASON_SHIFT_ENDED in result.reasons


def test_inside_the_scheduled_shift_is_accepted() -> None:
    user = make_user()
    membership = make_membership(
        user,
        shift_start=dt_time(9, 0),
        shift_end=dt_time(18, 0),
    )
    result = evaluate(make_candidate(user, membership), now=NOW)
    assert result.eligible


def test_shift_day_restriction_is_honoured() -> None:
    """``shift_days`` holds weekday numbers where Monday is 0."""
    user = make_user()
    tuesday = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    assert tuesday.weekday() == 1
    membership = make_membership(
        user,
        shift_start=dt_time(9, 0),
        shift_end=dt_time(18, 0),
        shift_days=[0, 2, 3, 4],  # Monday, Wednesday, Thursday, Friday
    )
    result = evaluate(make_candidate(user, membership), now=tuesday)
    assert alloc.REASON_SHIFT_ENDED in result.reasons


def test_overnight_shift_covers_both_ends_of_the_day() -> None:
    """A 22:00-06:00 shift spans midnight, so it must not read as empty."""
    user = make_user()
    membership = make_membership(user, shift_start=dt_time(22, 0), shift_end=dt_time(6, 0))
    assert alloc._shift_covers(membership, datetime(2026, 9, 29, 23, 0, tzinfo=UTC))
    assert alloc._shift_covers(membership, datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
    assert not alloc._shift_covers(membership, datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    assert not alloc._shift_covers(membership, datetime(2026, 9, 29, 21, 0, tzinfo=UTC))
    # 10:00 (NOW) is outside it, so allocation must be rejected.
    result = evaluate(make_candidate(user, membership), now=NOW)
    assert alloc.REASON_SHIFT_ENDED in result.reasons


def test_all_reasons_are_collected_not_just_the_first() -> None:
    user = make_user(status=OperationalStatus.INACTIVE, is_available=False)
    membership = make_membership(user, is_active=False, capacity=1)
    result = evaluate(make_candidate(user, membership, open_workload=5))
    assert len(result.reasons) >= 4
    assert alloc.REASON_QUEUE_INACTIVE in result.reasons
    assert alloc.REASON_USER_INACTIVE in result.reasons
    assert alloc.REASON_UNAVAILABLE in result.reasons
    assert alloc.REASON_AT_CAPACITY in result.reasons


def test_lowest_workload_wins() -> None:
    busy = make_user(name="Busy")
    quiet = make_user(name="Quiet")
    results = [
        evaluate(make_candidate(busy, make_membership(busy), open_workload=5)),
        evaluate(make_candidate(quiet, make_membership(quiet), open_workload=1)),
    ]
    chosen = alloc.select(results)
    assert chosen is not None
    assert chosen.candidate.display_name == "Quiet"


def test_workload_ties_break_on_priority_weight_then_name() -> None:
    low_weight = make_user(name="Zoe")
    high_weight = make_user(name="Amy")
    results = [
        evaluate(make_candidate(low_weight, make_membership(low_weight, priority_weight=10))),
        evaluate(make_candidate(high_weight, make_membership(high_weight, priority_weight=90))),
    ]
    chosen = alloc.select(results)
    assert chosen is not None
    assert chosen.candidate.display_name == "Amy"


def test_identical_candidates_break_on_username_so_the_pick_is_stable() -> None:
    first = make_user(name="Same One A")
    second = make_user(name="Same One B")
    first.username = "aaa"
    second.username = "bbb"
    results = [
        evaluate(make_candidate(first, make_membership(first))),
        evaluate(make_candidate(second, make_membership(second))),
    ]
    picks = {alloc.select(results).candidate.username for _ in range(10)}  # type: ignore[union-attr]
    assert picks == {"aaa"}


def test_select_returns_none_when_nobody_is_eligible() -> None:
    user = make_user(status=OperationalStatus.INACTIVE)
    assert alloc.select([evaluate(make_candidate(user, make_membership(user)))]) is None


def test_select_ignores_ineligible_candidates() -> None:
    good = make_user(name="Good")
    bad = make_user(status=OperationalStatus.INACTIVE, name="Bad")
    results = [
        evaluate(make_candidate(bad, make_membership(bad))),
        evaluate(make_candidate(good, make_membership(good))),
    ]
    chosen = alloc.select(results)
    assert chosen is not None
    assert chosen.candidate.display_name == "Good"


def test_rejection_summary_counts_members() -> None:
    first = make_user(status=OperationalStatus.INACTIVE, name="A")
    second = make_user(status=OperationalStatus.INACTIVE, name="B")
    results = [
        evaluate(make_candidate(first, make_membership(first))),
        evaluate(make_candidate(second, make_membership(second))),
    ]
    summary = alloc.rejection_summary(results)
    assert any(alloc.REASON_USER_INACTIVE in line and "2 members" in line for line in summary)


def test_rejection_summary_when_nobody_was_even_considered() -> None:
    assert alloc.rejection_summary([]) == ["no eligible queue members"]


def test_explanation_mentions_the_chosen_person_and_the_factors() -> None:
    queue = make_queue()
    user = make_user(name="Meera R")
    result = evaluate(make_candidate(user, make_membership(user), open_workload=2))
    chosen = alloc.select([result])
    assert chosen is not None
    factors = alloc.build_factors(chosen, queue, lowest_workload=2)
    explanation = alloc.AllocationDecision(
        chosen=chosen,
        considered=1,
        eligible_count=1,
        factors=factors,
        rejected_reasons=(),
    ).explanation()
    assert "Meera R" in explanation
    assert "workload 2" in explanation
    assert "capacity" in explanation


def test_open_statuses_excludes_terminal_states() -> None:
    """A guard on the constant the rest of the suite leans on."""
    names = {getattr(item, "value", item) for item in OPEN_STATUSES}
    assert names
    assert "CLOSED" not in names
    assert "RESOLVED" not in names


def test_effective_capacity_prefers_the_membership_override() -> None:
    user = make_user()
    queue = make_queue(max_workload_per_user=10)
    override = evaluate(make_candidate(user, make_membership(user, capacity=3)), queue=queue)
    fallback = evaluate(make_candidate(user, make_membership(user, capacity=None)), queue=queue)
    assert override.effective_capacity == 3
    assert fallback.effective_capacity == 10


@pytest.mark.parametrize("delta", [0, 1, 2, 3])
def test_daily_limit_boundary(delta: int) -> None:
    user = make_user(daily_limit=5)
    result = evaluate(make_candidate(user, make_membership(user), daily_allocations_today=delta))
    assert result.eligible is (delta < 5)


def test_reasons_never_duplicate() -> None:
    user = make_user(status=OperationalStatus.INACTIVE)
    result = evaluate(make_candidate(user, make_membership(user, is_active=False)))
    assert len(result.reasons) == len(set(result.reasons))


def test_unavailable_until_overrides_the_available_flag() -> None:
    """A window that ends in the future still blocks allocation."""
    user = make_user()
    candidate = make_candidate(
        user, make_membership(user), unavailable_until=NOW + timedelta(minutes=30)
    )
    result = evaluate(candidate)
    assert alloc.REASON_UNAVAILABLE in result.reasons
