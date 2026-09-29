"""SLA computation and classification (PRD 16, 22).

The SLA clock is derived, never stored as a countdown, so these tests pin the
arithmetic and the state machine rather than any particular row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.sla import SlaBreachAction, SlaPolicy, SlaStatus
from app.models.work import WorkItem, WorkStatus
from app.services import sla as sla_service

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def make_policy(*, target: int = 240, warning: int = 60) -> SlaPolicy:
    return SlaPolicy(
        id=uuid.uuid4(),
        tenant_id="BPSEC",
        code="URGENT_4H",
        name="Urgent - 4 hours",
        target_minutes=target,
        warning_minutes=warning,
        breach_action=SlaBreachAction.ESCALATE,
        is_active=True,
    )


def make_item(
    *,
    status: WorkStatus = WorkStatus.OPEN,
    due_at: datetime | None = None,
    assigned_at: datetime | None = None,
    closed_at: datetime | None = None,
    breached_at: datetime | None = None,
) -> WorkItem:
    """An in-memory work item; nothing here touches the database."""
    return WorkItem(
        id=uuid.uuid4(),
        tenant_id="BPSEC",
        source_app="OPS",
        source_key="LOS:APP1",
        title="Card not reflecting credit limit",
        queue_code="LOS",
        status=status,
        due_at=due_at,
        assigned_at=assigned_at,
        closed_at=closed_at,
        sla_breached_at=breached_at,
    )


# --------------------------------------------------------------------------
# due_at
# --------------------------------------------------------------------------


def test_due_at_is_created_at_plus_the_target() -> None:
    policy = make_policy(target=240)
    assert sla_service.compute_due_at(created_at=NOW, policy=policy) == NOW + timedelta(minutes=240)


def test_due_at_is_none_without_a_policy() -> None:
    assert sla_service.compute_due_at(created_at=NOW, policy=None) is None


def test_due_at_is_timezone_aware_arithmetic() -> None:
    """created_at is stored tz-aware; the offset must not be lost."""
    created = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    due = sla_service.compute_due_at(created_at=created, policy=make_policy(target=15))
    assert due == datetime(2026, 9, 29, 10, 15, tzinfo=UTC)
    assert due.tzinfo is not None


# --------------------------------------------------------------------------
# compute_status
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining_minutes", "expected"),
    [
        (200, SlaStatus.ON_TRACK),
        (61, SlaStatus.ON_TRACK),
        (60, SlaStatus.DUE_SOON),
        (30, SlaStatus.DUE_SOON),
        (1, SlaStatus.DUE_SOON),
        (0, SlaStatus.BREACHED),
        (-1, SlaStatus.BREACHED),
    ],
)
def test_status_is_derived_from_the_remaining_time(
    remaining_minutes: int, expected: SlaStatus
) -> None:
    status = sla_service.compute_status(
        due_at=NOW + timedelta(minutes=remaining_minutes),
        status=WorkStatus.ASSIGNED,
        started_at=NOW - timedelta(hours=1),
        closed_at=None,
        breached_at=None,
        now=NOW,
        warning_minutes=60,
    )
    assert status is expected


def test_unassigned_item_with_a_deadline_is_on_track() -> None:
    """``NOT_STARTED`` means "no deadline to miss", not "nobody picked it up".

    An unassigned case with four hours left is meeting its target, and the
    workbench surfaces unassigned work through ``WorkStatus`` / workload
    rather than through the SLA state.
    """
    status = sla_service.compute_status(
        due_at=NOW + timedelta(hours=5),
        status=WorkStatus.OPEN,
        started_at=None,
        closed_at=None,
        breached_at=None,
        now=NOW,
    )
    assert status is SlaStatus.ON_TRACK


def test_only_a_deadgeless_unassigned_item_is_not_started() -> None:
    status = sla_service.compute_status(
        due_at=None,
        status=WorkStatus.OPEN,
        started_at=None,
        closed_at=None,
        breached_at=None,
        now=NOW,
    )
    assert status is SlaStatus.NOT_STARTED


def test_item_without_a_deadline_tracks_assignment_only() -> None:
    unassigned = sla_service.compute_status(
        due_at=None,
        status=WorkStatus.OPEN,
        started_at=None,
        closed_at=None,
        breached_at=None,
        now=NOW,
    )
    assigned = sla_service.compute_status(
        due_at=None,
        status=WorkStatus.ASSIGNED,
        started_at=NOW - timedelta(minutes=5),
        closed_at=None,
        breached_at=None,
        now=NOW,
    )
    assert unassigned is SlaStatus.NOT_STARTED
    assert assigned is SlaStatus.ON_TRACK


def test_warning_window_comes_from_the_supplied_policy() -> None:
    """A three-hour warning fires when two hours are left on a five-hour target."""
    status = sla_service.compute_status(
        due_at=NOW + timedelta(hours=2),
        status=WorkStatus.IN_PROGRESS,
        started_at=NOW - timedelta(hours=3),
        closed_at=None,
        breached_at=None,
        now=NOW,
        warning_minutes=180,
    )
    assert status is SlaStatus.DUE_SOON


def test_a_wider_policy_makes_the_same_deadline_on_track() -> None:
    """Same inputs, narrower warning window -> the item is not yet due soon."""
    status = sla_service.compute_status(
        due_at=NOW + timedelta(hours=4),
        status=WorkStatus.IN_PROGRESS,
        started_at=NOW - timedelta(hours=1),
        closed_at=None,
        breached_at=None,
        now=NOW,
        warning_minutes=15,
    )
    assert status is SlaStatus.ON_TRACK


def test_warning_window_falls_back_to_configuration() -> None:
    from app.config import settings

    window = settings.ops_sla_due_soon_minutes
    due_at = NOW + timedelta(minutes=window)
    status = sla_service.compute_status(
        due_at=due_at,
        status=WorkStatus.ASSIGNED,
        started_at=NOW,
        closed_at=None,
        breached_at=None,
        now=NOW,
        warning_minutes=None,
    )
    assert status is SlaStatus.DUE_SOON


# --------------------------------------------------------------------------
# terminal states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [WorkStatus.CLOSED, WorkStatus.CANCELLED])
def test_terminal_status_completes_even_after_the_deadline_passed(status: WorkStatus) -> None:
    resolved = sla_service.compute_status(
        due_at=NOW - timedelta(days=5),
        status=status,
        started_at=NOW - timedelta(days=6),
        closed_at=NOW,
        breached_at=None,
        now=NOW,
    )
    assert resolved is SlaStatus.COMPLETED


def test_closed_wins_over_an_existing_breach() -> None:
    """A case closed after breaching reports COMPLETED, not BREACHED."""
    resolved = sla_service.compute_status(
        due_at=NOW - timedelta(days=5),
        status=WorkStatus.CLOSED,
        started_at=NOW - timedelta(days=6),
        closed_at=NOW,
        breached_at=NOW - timedelta(days=5),
        now=NOW,
    )
    assert resolved is SlaStatus.COMPLETED


def test_a_recorded_breach_is_sticky_before_the_deadline_is_reached() -> None:
    """Reopening must not silently clear evidence that the target was missed."""
    status = sla_service.compute_status(
        due_at=NOW + timedelta(hours=3),
        status=WorkStatus.ASSIGNED,
        started_at=NOW - timedelta(hours=2),
        closed_at=None,
        breached_at=NOW - timedelta(minutes=30),
        now=NOW,
    )
    assert status is SlaStatus.BREACHED


# --------------------------------------------------------------------------
# resolve_for_item
# --------------------------------------------------------------------------


def test_resolution_reports_target_and_warning_from_the_policy() -> None:
    item = make_item(
        status=WorkStatus.IN_PROGRESS,
        due_at=NOW + timedelta(minutes=30),
        assigned_at=NOW - timedelta(hours=2),
    )
    resolution = sla_service.resolve_for_item(
        item, policy=make_policy(target=240, warning=60), now=NOW
    )
    assert resolution.status is SlaStatus.DUE_SOON
    assert resolution.target_minutes == 240
    assert resolution.warning_minutes == 60
    assert resolution.due_at == item.due_at
    assert resolution.breached_at is None


def test_resolution_stamps_the_deadline_as_the_breach_time() -> None:
    """The breach is observed late, but the target was missed at ``due_at``."""
    due_at = NOW - timedelta(hours=2)
    item = make_item(status=WorkStatus.IN_PROGRESS, due_at=due_at)
    resolution = sla_service.resolve_for_item(item, now=NOW)
    assert resolution.status is SlaStatus.BREACHED
    assert resolution.breached_at == due_at


def test_resolution_leaves_an_existing_breach_timestamp_alone() -> None:
    """The first observation wins; later reads must not rewrite the evidence."""
    recorded = NOW - timedelta(hours=6)
    item = make_item(
        status=WorkStatus.IN_PROGRESS,
        due_at=NOW - timedelta(hours=8),
        breached_at=recorded,
    )
    resolution = sla_service.resolve_for_item(item, now=NOW)
    assert resolution.breached_at == recorded
    # resolve_for_item is read-only: it must not stamp the row itself.
    assert item.sla_breached_at == recorded


def test_resolution_without_a_policy_leaves_target_unset() -> None:
    item = make_item(due_at=NOW + timedelta(hours=2))
    resolution = sla_service.resolve_for_item(item, now=NOW)
    assert resolution.target_minutes is None
    assert resolution.warning_minutes is None
    assert resolution.status is SlaStatus.ON_TRACK


# --------------------------------------------------------------------------
# mark_breaches
# --------------------------------------------------------------------------


def test_mark_breaches_returns_only_newly_missed_items() -> None:
    fresh = make_item(due_at=NOW - timedelta(minutes=1))
    already = make_item(due_at=NOW - timedelta(hours=3), breached_at=NOW - timedelta(hours=3))
    future = make_item(due_at=NOW + timedelta(hours=1))
    assert sla_service.mark_breaches([fresh, already, future], now=NOW) == [fresh]


def test_mark_breaches_ignores_closed_and_cancelled_items() -> None:
    closed = make_item(status=WorkStatus.CLOSED, due_at=NOW - timedelta(days=2))
    cancelled = make_item(status=WorkStatus.CANCELLED, due_at=NOW - timedelta(days=2))
    live = make_item(due_at=NOW - timedelta(days=2))
    result = sla_service.mark_breaches([closed, cancelled, live], now=NOW)
    assert result == [live]


def test_mark_breaches_ignores_items_with_no_deadline() -> None:
    assert sla_service.mark_breaches([make_item(due_at=None)], now=NOW) == []


def test_mark_breaches_is_idempotent() -> None:
    item = make_item(due_at=NOW - timedelta(minutes=1))
    assert sla_service.mark_breaches([item], now=NOW) == [item]
    item.sla_breached_at = NOW - timedelta(minutes=1)
    assert sla_service.mark_breaches([item], now=NOW + timedelta(hours=1)) == []


# --------------------------------------------------------------------------
# seconds_remaining
# --------------------------------------------------------------------------


def test_seconds_remaining_is_positive_before_the_deadline() -> None:
    due_at = NOW + timedelta(minutes=10)
    assert sla_service.seconds_remaining(due_at, now=NOW) == 600


def test_seconds_remaining_is_negative_after_the_deadline() -> None:
    due_at = NOW - timedelta(minutes=10)
    assert sla_service.seconds_remaining(due_at, now=NOW) == -600


def test_seconds_remaining_truncates_towards_zero() -> None:
    """Sub-second remainders floor to 0 rather than leaking a -1 countdown."""
    due_at = NOW + timedelta(milliseconds=200)
    assert sla_service.seconds_remaining(due_at, now=NOW) == 0


def test_seconds_remaining_is_none_without_a_deadline() -> None:
    assert sla_service.seconds_remaining(None, now=NOW) is None


# --------------------------------------------------------------------------
# ageing buckets
# --------------------------------------------------------------------------


def test_ageing_buckets_are_ordered_and_contiguous() -> None:
    boundaries = [bucket[1] for bucket in sla_service.AGEING_BUCKETS]
    assert boundaries == sorted(boundaries)
    assert boundaries[0] == 0
    # Every bucket's minimum is the previous bucket's maximum, except the last.
    for previous, current in zip(
        sla_service.AGEING_BUCKETS, sla_service.AGEING_BUCKETS[1:], strict=False
    ):
        assert previous[2] == current[1]


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (0, "< 4 hours"),
        (3.9, "< 4 hours"),
        (4, "4 - 24 hours"),
        (23.9, "4 - 24 hours"),
        (24, "1 - 3 days"),
        (71, "1 - 3 days"),
        (72, "3 - 7 days"),
        (167, "3 - 7 days"),
        (168, "7 - 30 days"),
        (719, "7 - 30 days"),
        (720, "> 30 days"),
        (100_000, "> 30 days"),
    ],
)
def test_bucket_for_age(hours: float, expected: str) -> None:
    assert sla_service.bucket_for_age(hours) == expected


def test_every_bucket_label_is_reachable() -> None:
    labels = {
        sla_service.bucket_for_age(float(minimum)) for _, minimum, _ in sla_service.AGEING_BUCKETS
    }
    assert labels == {label for label, _, _ in sla_service.AGEING_BUCKETS}


# --------------------------------------------------------------------------
# policy-level invariants
# --------------------------------------------------------------------------


def test_policy_rejects_a_warning_longer_than_the_target() -> None:
    """Pinned against the database CHECK constraint of the same name."""
    policy = make_policy(target=60, warning=90)
    assert policy.warning_minutes > policy.target_minutes

    table = SlaPolicy.__table__
    names = {constraint.name for constraint in table.constraints if constraint.name}
    assert "ck_sla_policies_warning_before_target" in names
    assert "ck_sla_policies_target_positive" in names


def test_breach_action_is_carried_on_the_policy() -> None:
    assert make_policy().breach_action is SlaBreachAction.ESCALATE
