"""Availability requests and the self-approval rule (PRD 14).

The rule under test is simple to state and easy to regress: *nobody approves
their own availability*. It is enforced in three independent places, and all
three are covered here, because any one of them being wrong is a control
failure:

1. the service layer, when a request is created already-approved;
2. the service layer, when somebody tries to approve a request they raised;
3. a database CHECK constraint, so a future code path cannot bypass it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, ErrorCode, NotFoundError, PermissionDeniedError
from app.models.audit import AuditAction
from app.models.availability import (
    AvailabilityRequestStatus,
    AvailabilityType,
    UserAvailability,
)
from app.models.user import User
from app.schemas.org import AvailabilityCreate, AvailabilityUpdate
from app.services import availability as service
from app.services.org import WriteContext
from tests.conftest import TENANT_A, TENANT_B
from tests.factories import audit_rows, hours_from_now, make_user

pytestmark = pytest.mark.usefixtures("engine")


def _ctx(actor: User, **permissions: str) -> WriteContext:
    return WriteContext(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.id,
        actor_name=actor.display_name,
        permissions=frozenset(permissions),
    )


def _request(session: Session, user: User, **overrides: Any) -> AvailabilityCreate:
    payload: dict[str, Any] = {
        "user_id": user.id,
        "availability_type": AvailabilityType.LEAVE,
        "start_at": hours_from_now(24),
        "end_at": hours_from_now(48),
        "reason": "Family function",
    }
    payload.update(overrides)
    return AvailabilityCreate(**payload)


# --------------------------------------------------------------------------
# self approval, layer 1: creation never decides anything
# --------------------------------------------------------------------------


def test_creating_a_request_always_starts_in_requested(session: Session) -> None:
    """Even a holder of the approve permission cannot create a decided row.

    There is deliberately no ``submit_for_approval`` escape hatch. Creation has
    exactly one outcome, so ``created_by_user_id == decided_by_user_id`` is
    unrepresentable and the ``no_self_approval`` constraint can never fire.
    """
    lead = make_user(session, roles=("TEAM_LEAD",))
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, lead),
        ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert window.status is AvailabilityRequestStatus.REQUESTED
    assert window.decided_by_user_id is None
    assert window.decided_by_name is None
    assert window.decided_at is None
    assert window.created_by_user_id == lead.id


def test_an_admin_cannot_create_a_pre_approved_request(session: Session) -> None:
    """Full administrative rights do not extend to skipping the approval step."""
    admin = make_user(session, roles=("OPS_ADMIN",))
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, admin),
        ctx=_ctx(admin, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert window.status is AvailabilityRequestStatus.REQUESTED
    assert window.decided_by_user_id is None


def test_a_manager_may_raise_a_request_for_somebody_else(session: Session) -> None:
    """Raising on a report's behalf is fine; deciding it is somebody else's job."""
    manager = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, agent),
        ctx=_ctx(manager, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert window.user_id == agent.id
    assert window.created_by_user_id == manager.id
    assert window.decided_by_user_id is None
    assert window.status is AvailabilityRequestStatus.REQUESTED


def test_a_manager_may_not_raise_and_approve_in_one_step(session: Session) -> None:
    """The raise-then-approve path is closed by ``no_self_approval``, not by luck.

    ``approve_availability`` refuses because the actor raised it, and the
    database constraint independently refuses a row whose creator is also its
    decider -- so the two layers agree rather than one masking the other.
    """
    manager = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, agent),
        ctx=_ctx(manager, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    with pytest.raises(PermissionDeniedError) as caught:
        service.approve_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            reason="Approved on the spot",
            ctx=_ctx(manager, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
        )
    assert caught.value.code == ErrorCode.SELF_APPROVAL_NOT_ALLOWED
    assert window.status is AvailabilityRequestStatus.REQUESTED


# --------------------------------------------------------------------------
# self approval, layer 2: approving a request you raised
# --------------------------------------------------------------------------


def test_you_cannot_approve_a_request_you_raised_for_yourself(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, lead),
        ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert window.status is AvailabilityRequestStatus.REQUESTED

    with pytest.raises(PermissionDeniedError) as caught:
        service.approve_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            reason="Approved",
            ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
        )
    assert caught.value.code == ErrorCode.SELF_APPROVAL_NOT_ALLOWED
    assert window.status is AvailabilityRequestStatus.REQUESTED


def test_you_cannot_approve_a_request_you_raised_for_a_colleague(session: Session) -> None:
    """Raising the request is as disqualifying as being the subject of it."""
    lead = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, agent),
        ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    with pytest.raises(PermissionDeniedError) as caught:
        service.approve_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            reason="Approved",
            ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
        )
    assert caught.value.code == ErrorCode.SELF_APPROVAL_NOT_ALLOWED


def test_you_cannot_reject_a_request_you_raised(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, agent), ctx=_ctx(lead)
    )
    with pytest.raises(PermissionDeniedError) as caught:
        service.reject_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            reason="No",
            ctx=_ctx(lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
        )
    assert caught.value.code == ErrorCode.SELF_APPROVAL_NOT_ALLOWED


def test_a_second_lead_can_approve_what_a_first_lead_raised(session: Session) -> None:
    raiser = make_user(session, roles=("TEAM_LEAD",))
    approver = make_user(session, roles=("TEAM_LEAD",))
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, raiser),
        ctx=_ctx(raiser, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    approved = service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="Cover arranged",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert approved.status is AvailabilityRequestStatus.APPROVED
    assert approved.decided_by_user_id == approver.id
    assert approved.decision_reason == "Cover arranged"


# --------------------------------------------------------------------------
# self approval, layer 3: the database
# --------------------------------------------------------------------------


def test_the_database_refuses_a_row_where_the_approver_is_the_raiser(session: Session) -> None:
    """Belt and braces: a raw INSERT that self-approves must fail."""
    person = make_user(session)
    session.add(
        UserAvailability(
            tenant_id=TENANT_A,
            user_id=person.id,
            availability_type=AvailabilityType.LEAVE,
            status=AvailabilityRequestStatus.APPROVED,
            start_at=hours_from_now(1),
            end_at=hours_from_now(2),
            created_by_user_id=person.id,
            created_by_name=person.display_name,
            decided_by_user_id=person.id,
            decided_by_name=person.display_name,
            decided_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError) as caught:
        session.flush()
    assert "no_self_approval" in str(caught.value)
    session.rollback()


def test_the_constraint_allows_a_pending_request_from_the_raiser(session: Session) -> None:
    """The constraint must not block the normal path."""
    person = make_user(session)
    window = UserAvailability(
        tenant_id=TENANT_A,
        user_id=person.id,
        availability_type=AvailabilityType.LEAVE,
        status=AvailabilityRequestStatus.REQUESTED,
        start_at=hours_from_now(1),
        end_at=hours_from_now(2),
        created_by_user_id=person.id,
        created_by_name=person.display_name,
    )
    session.add(window)
    session.flush()
    assert window.decided_by_user_id is None


def test_the_database_refuses_a_window_that_ends_before_it_starts(session: Session) -> None:
    person = make_user(session)
    session.add(
        UserAvailability(
            tenant_id=TENANT_A,
            user_id=person.id,
            availability_type=AvailabilityType.LEAVE,
            status=AvailabilityRequestStatus.REQUESTED,
            start_at=hours_from_now(5),
            end_at=hours_from_now(1),
            created_by_user_id=person.id,
            created_by_name=person.display_name,
        )
    )
    with pytest.raises(IntegrityError) as caught:
        session.flush()
    assert "end_after_start" in str(caught.value)
    session.rollback()


# --------------------------------------------------------------------------
# window validation
# --------------------------------------------------------------------------


def test_a_window_must_end_after_it_starts(session: Session) -> None:
    user = make_user(session)
    with pytest.raises(ConflictError) as caught:
        service.create_availability(
            session,
            tenant_id=TENANT_A,
            data=_request(session, user, start_at=hours_from_now(5), end_at=hours_from_now(5)),
            ctx=_ctx(user),
        )
    assert caught.value.code == "INVALID_WINDOW"


def test_a_window_cannot_exceed_a_year(session: Session) -> None:
    user = make_user(session)
    with pytest.raises(ConflictError) as caught:
        service.create_availability(
            session,
            tenant_id=TENANT_A,
            data=_request(
                session, user, start_at=hours_from_now(1), end_at=hours_from_now(24 * 400)
            ),
            ctx=_ctx(user),
        )
    assert caught.value.code == "INVALID_WINDOW"


def test_overlapping_requests_are_refused(session: Session) -> None:
    user = make_user(session)
    service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, user, start_at=hours_from_now(24), end_at=hours_from_now(48)),
        ctx=_ctx(user),
    )
    with pytest.raises(ConflictError) as caught:
        service.create_availability(
            session,
            tenant_id=TENANT_A,
            data=_request(session, user, start_at=hours_from_now(36), end_at=hours_from_now(60)),
            ctx=_ctx(user),
        )
    assert caught.value.code == "OVERLAPPING_AVAILABILITY"
    assert "conflicting_request_id" in caught.value.details


def test_abutting_windows_are_allowed(session: Session) -> None:
    """Ending exactly when the next starts is not an overlap."""
    user = make_user(session)
    service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, user, start_at=hours_from_now(24), end_at=hours_from_now(48)),
        ctx=_ctx(user),
    )
    second = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, user, start_at=hours_from_now(48), end_at=hours_from_now(72)),
        ctx=_ctx(user),
    )
    assert second.id is not None


def test_a_cancelled_window_does_not_block_a_new_request(session: Session) -> None:
    user = make_user(session)
    first = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, user, start_at=hours_from_now(24), end_at=hours_from_now(48)),
        ctx=_ctx(user),
    )
    service.cancel_availability(
        session, tenant_id=TENANT_A, availability_id=first.id, reason="Not needed", ctx=_ctx(user)
    )
    again = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, user, start_at=hours_from_now(24), end_at=hours_from_now(48)),
        ctx=_ctx(user),
    )
    assert again.id != first.id


# --------------------------------------------------------------------------
# editing and re-approval
# --------------------------------------------------------------------------


def test_editing_an_approved_window_returns_it_to_requested(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    other_lead = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, agent),
        ctx=_ctx(lead),
    )
    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="ok",
        ctx=_ctx(other_lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    decided_by = window.decided_by_user_id
    decided_at = window.decided_at

    edited = service.update_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        data=AvailabilityUpdate(end_at=hours_from_now(72), change_reason="Longer trip"),
        ctx=_ctx(other_lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert edited.status is AvailabilityRequestStatus.REQUESTED
    # Provenance is evidence: re-approval must not erase the earlier decision.
    assert edited.decided_by_user_id == decided_by
    assert edited.decided_at == decided_at
    assert edited.edit_count == 1


def test_the_requester_cannot_edit_an_approved_window(session: Session) -> None:
    other_lead = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, agent), ctx=_ctx(agent)
    )
    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="ok",
        ctx=_ctx(other_lead, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    with pytest.raises(PermissionDeniedError):
        service.update_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            data=AvailabilityUpdate(end_at=hours_from_now(72)),
            ctx=_ctx(agent),
        )


def test_a_cancelled_request_cannot_be_edited(session: Session) -> None:
    user = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, user), ctx=_ctx(user)
    )
    service.cancel_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="Changed plans",
        ctx=_ctx(user),
    )
    with pytest.raises(ConflictError) as caught:
        service.update_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            data=AvailabilityUpdate(end_at=hours_from_now(96)),
            ctx=_ctx(user),
        )
    assert caught.value.code == "ALREADY_CANCELLED"


def test_deciding_an_already_decided_request_is_refused(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    approver = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, agent), ctx=_ctx(lead)
    )
    ctx = _ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE")
    service.approve_availability(
        session, tenant_id=TENANT_A, availability_id=window.id, reason="ok", ctx=ctx
    )
    with pytest.raises(ConflictError) as caught:
        service.approve_availability(
            session, tenant_id=TENANT_A, availability_id=window.id, reason="again", ctx=ctx
        )
    assert caught.value.code == "INVALID_STATE_TRANSITION"


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------


def test_a_stranger_cannot_cancel_your_request(session: Session) -> None:
    owner = make_user(session)
    stranger = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, owner), ctx=_ctx(owner)
    )
    with pytest.raises(PermissionDeniedError):
        service.cancel_availability(
            session,
            tenant_id=TENANT_A,
            availability_id=window.id,
            reason="not mine",
            ctx=_ctx(stranger),
        )


def test_an_approver_can_cancel_anybody_s_request(session: Session) -> None:
    owner = make_user(session)
    approver = make_user(session, roles=("TEAM_LEAD",))
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, owner), ctx=_ctx(owner)
    )
    cancelled = service.cancel_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="No longer needed",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert cancelled.status is AvailabilityRequestStatus.CANCELLED


def test_cancelling_twice_is_idempotent(session: Session) -> None:
    owner = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, owner), ctx=_ctx(owner)
    )
    first = service.cancel_availability(
        session, tenant_id=TENANT_A, availability_id=window.id, reason="x", ctx=_ctx(owner)
    )
    second = service.cancel_availability(
        session, tenant_id=TENANT_A, availability_id=window.id, reason="x", ctx=_ctx(owner)
    )
    assert first.id == second.id
    assert second.status is AvailabilityRequestStatus.CANCELLED


# --------------------------------------------------------------------------
# availability flag
# --------------------------------------------------------------------------


def test_an_approved_blocking_window_marks_the_user_unavailable(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    approver = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(
            session,
            agent,
            start_at=datetime.now(UTC) - timedelta(hours=1),
            end_at=hours_from_now(4),
        ),
        ctx=_ctx(lead),
    )
    assert agent.is_available is True

    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="ok",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    session.refresh(agent)
    assert agent.is_available is False
    assert agent.availability_ends_at is not None


def test_a_break_does_not_mark_the_user_unavailable(session: Session) -> None:
    """Staff on a break are still on shift and can still take work."""
    approver = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(
            session,
            agent,
            availability_type=AvailabilityType.BREAK,
            start_at=datetime.now(UTC) - timedelta(minutes=5),
            end_at=hours_from_now(1),
        ),
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    session.refresh(agent)
    assert agent.is_available is True


def test_cancelling_the_window_restores_availability(session: Session) -> None:
    approver = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    window = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(
            session,
            agent,
            start_at=datetime.now(UTC) - timedelta(hours=1),
            end_at=hours_from_now(4),
        ),
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    session.refresh(agent)
    assert agent.is_available is False

    service.cancel_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="Back early",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    session.refresh(agent)
    assert agent.is_available is True
    assert agent.availability_ends_at is None


def test_only_approved_windows_block_allocation(session: Session) -> None:
    approver = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    pending = service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(
            session,
            agent,
            start_at=datetime.now(UTC) - timedelta(hours=1),
            end_at=hours_from_now(4),
        ),
        ctx=_ctx(agent),
    )
    assert service.unavailable_user_ids(session, tenant_id=TENANT_A) == {}

    approver_ctx = _ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE")
    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=pending.id,
        reason="ok",
        ctx=approver_ctx,
    )
    blocking = service.unavailable_user_ids(session, tenant_id=TENANT_A)
    assert set(blocking) == {agent.id}


def test_a_future_window_does_not_block_now(session: Session) -> None:
    approver = make_user(session, roles=("OPS_MANAGER",))
    agent = make_user(session)
    service.create_availability(
        session,
        tenant_id=TENANT_A,
        data=_request(session, agent, start_at=hours_from_now(48), end_at=hours_from_now(72)),
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    assert service.unavailable_user_ids(session, tenant_id=TENANT_A) == {}


# --------------------------------------------------------------------------
# tenant isolation
# --------------------------------------------------------------------------


def test_a_request_from_another_tenant_is_not_found(session: Session) -> None:
    agent = make_user(session, tenant_id=TENANT_B)
    window = service.create_availability(
        session,
        tenant_id=TENANT_B,
        data=_request(session, agent),
        ctx=_ctx(agent),
    )
    with pytest.raises(NotFoundError):
        service.get_availability(session, tenant_id=TENANT_A, availability_id=window.id)


def test_listing_is_tenant_scoped(session: Session) -> None:
    mine = make_user(session, tenant_id=TENANT_A)
    theirs = make_user(session, tenant_id=TENANT_B)
    service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, mine), ctx=_ctx(mine)
    )
    service.create_availability(
        session, tenant_id=TENANT_B, data=_request(session, theirs), ctx=_ctx(theirs)
    )
    rows, total = service.list_availability(session, tenant_id=TENANT_A)
    assert total == 1
    assert {row.user_id for row in rows} == {mine.id}


def test_pending_approval_count_is_tenant_scoped(session: Session) -> None:
    mine = make_user(session, tenant_id=TENANT_A)
    theirs = make_user(session, tenant_id=TENANT_B)
    service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, mine), ctx=_ctx(mine)
    )
    service.create_availability(
        session, tenant_id=TENANT_B, data=_request(session, theirs), ctx=_ctx(theirs)
    )
    assert service.pending_approval_count(session, tenant_id=TENANT_A) == 1
    assert service.pending_approval_count(session, tenant_id=TENANT_B) == 1


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def test_the_full_availability_lifecycle_is_audited(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    approver = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)

    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, agent), ctx=_ctx(lead)
    )
    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="Cover arranged",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    service.update_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        data=AvailabilityUpdate(end_at=hours_from_now(96), change_reason="Extended"),
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    service.cancel_availability(
        session, tenant_id=TENANT_A, availability_id=window.id, reason="Back early", ctx=_ctx(agent)
    )

    actions = [row.action for row in audit_rows(session)]
    for expected in (
        AuditAction.AVAILABILITY_CREATED,
        AuditAction.AVAILABILITY_APPROVED,
        AuditAction.AVAILABILITY_UPDATED,
        AuditAction.AVAILABILITY_CANCELLED,
    ):
        assert expected in actions


def test_an_approval_audit_row_names_the_decision_reason(session: Session) -> None:
    lead = make_user(session, roles=("TEAM_LEAD",))
    approver = make_user(session, roles=("TEAM_LEAD",))
    agent = make_user(session)
    window = service.create_availability(
        session, tenant_id=TENANT_A, data=_request(session, agent), ctx=_ctx(lead)
    )
    service.approve_availability(
        session,
        tenant_id=TENANT_A,
        availability_id=window.id,
        reason="Cover arranged",
        ctx=_ctx(approver, OPS_AVAILABILITY_APPROVE="OPS_AVAILABILITY_APPROVE"),
    )
    row = next(
        entry for entry in audit_rows(session) if entry.action is AuditAction.AVAILABILITY_APPROVED
    )
    assert row.reason == "Cover arranged"
    assert row.actor_user_id == approver.id
    assert row.before_json is not None
    assert row.before_json["status"] == "REQUESTED"
    assert row.after_json is not None
    assert row.after_json["status"] == "APPROVED"
    assert set(row.changed_fields.split(",")) >= {"status", "decided_by_name", "decided_at"}


def test_creating_a_request_for_an_unknown_user_is_a_404(session: Session) -> None:
    manager = make_user(session, roles=("OPS_MANAGER",))
    with pytest.raises(NotFoundError):
        service.create_availability(
            session,
            tenant_id=TENANT_A,
            data=_request(session, manager, user_id=uuid.uuid4()),
            ctx=_ctx(manager),
        )
