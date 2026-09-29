"""Work item routes (PRD 38) - the Workbench backend.

Permission split:

===========================  =========================================
Operation                    Permission
===========================  =========================================
read / search                OPS_CASES_READ
create                       OPS_CASES_CREATE
manual assign                OPS_CASES_ASSIGN
auto assign                  OPS_CASES_ASSIGN
lock / heartbeat / release   OPS_CASES_LOCK
transfer                     OPS_CASES_TRANSFER
close                        OPS_CASES_CLOSE
comment                      OPS_CASES_COMMENT
override due date            OPS_CASES_ASSIGN
===========================  =========================================
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.errors import ValidationError
from app.models.sla import SlaStatus
from app.models.work import CaseComment, Priority, WorkItem, WorkStatus
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.work import (
    AssignRequest,
    AutoAssignResponse,
    CandidateItem,
    CloseRequest,
    CommentCreate,
    CommentDelete,
    CommentItem,
    CommentUpdate,
    DueAtOverride,
    EligibilityResponse,
    HeartbeatResponse,
    LockInfo,
    ManualAssignResponse,
    ReleaseLockRequest,
    SearchResponse,
    SearchResultItem,
    StatusChangeRequest,
    TransferRequest,
    UnassignRequest,
    WorkItemCreate,
    WorkItemDetail,
    WorkItemEventItem,
    WorkItemSummary,
)
from app.security.permissions import Permission
from app.services import work as work_service
from app.services.org import WriteContext

router = APIRouter(prefix="/work-items", tags=["work-items"])

CanRead = require_permissions(Permission.OPS_CASES_READ)
CanCreate = require_permissions(Permission.OPS_CASES_CREATE)
CanAssign = require_permissions(Permission.OPS_CASES_ASSIGN)
CanLock = require_permissions(Permission.OPS_CASES_LOCK)
CanTransfer = require_permissions(Permission.OPS_CASES_TRANSFER)
CanClose = require_permissions(Permission.OPS_CASES_CLOSE)
CanComment = require_permissions(Permission.OPS_CASES_COMMENT)


def _ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


def _lock_info(item: WorkItem, viewer_id: uuid.UUID) -> LockInfo:
    now = datetime.now(UTC)
    remaining: int | None = None
    if item.locked_until is not None:
        remaining = int((item.locked_until - now).total_seconds())
    return LockInfo(
        is_locked=item.is_locked and remaining is not None and remaining > 0,
        is_locked_by_me=item.locked_by_user_id == viewer_id,
        locked_by_user_id=item.locked_by_user_id,
        locked_by_name=item.locked_by_name,
        locked_at=item.locked_at,
        locked_until=item.locked_until,
        seconds_remaining=remaining,
        is_expired=item.is_locked and remaining is not None and remaining <= 0,
        last_heartbeat_at=item.last_heartbeat_at,
    )


def _summary(item: WorkItem, viewer_id: uuid.UUID) -> WorkItemSummary:
    now = datetime.now(UTC)
    return WorkItemSummary(
        id=item.id,
        source_app=item.source_app,
        source_key=item.source_key,
        external_ref=item.external_ref,
        deep_link=item.deep_link,
        title=item.title,
        customer_name=item.customer_name,
        customer_mobile_masked=item.customer_mobile_masked,
        queue_code=item.queue_code,
        department_code=item.department_code,
        status=item.status,
        priority=item.priority,
        sla_status=item.sla_status,
        assigned_user_id=item.assigned_user_id,
        assigned_user_name=item.assigned_user_name,
        due_at=item.due_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
        closed_at=item.closed_at,
        last_activity_at=item.last_activity_at,
        lock=_lock_info(item, viewer_id),
        age_minutes=int((now - item.created_at).total_seconds() // 60),
        is_overdue=bool(item.due_at and item.due_at < now and item.is_open),
    )


def _detail(session: Session, item: WorkItem, viewer_id: uuid.UUID) -> WorkItemDetail:
    comment_count = (
        session.scalar(
            select(func.count(CaseComment.id)).where(
                CaseComment.tenant_id == item.tenant_id,
                CaseComment.work_item_id == item.id,
                CaseComment.deleted_at.is_(None),
            )
        )
        or 0
    )

    base = _summary(item, viewer_id)
    return WorkItemDetail(
        **base.model_dump(),
        assigned_at=item.assigned_at,
        assignment_is_manual=item.assignment_is_manual,
        allocation_explanation=item.allocation_explanation,
        sla_breached_at=item.sla_breached_at,
        due_at_overridden=item.due_at_overridden,
        sla_policy_id=item.sla_policy_id,
        closure_outcome=item.closure_outcome,
        closure_notes=item.closure_notes,
        # ``payload_json`` is source controlled and already masked upstream.
        payload=item.payload_json or {},
        open_comment_count=comment_count,
        last_synced_at=item.last_synced_at,
        source_fields=item.ops_fields_json or {},
    )


# --------------------------------------------------------------------------
# list / read
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=PageResponse[WorkItemSummary],
    responses={403: {"model": ErrorResponse}},
    summary="List work items with server-side pagination and filtering",
)
def list_work_items(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    source_app: str | None = Query(default=None, max_length=16),
    queue_code: str | None = Query(default=None, max_length=64),
    department_code: str | None = Query(default=None, max_length=32),
    work_status: WorkStatus | None = Query(default=None, alias="status"),
    assigned_user_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
    unassigned: bool = False,
    priority: Priority | None = None,
    sla_state: SlaStatus | None = None,
    external_ref: str | None = Query(default=None, max_length=128),
    search: str | None = Query(default=None, max_length=150),
    overdue_only: bool = False,
    due_today: bool = False,
    locked_by_me: bool = False,
    locked: bool = False,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    age_min_hours: int | None = Query(default=None, ge=0, le=8760),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="created_at", max_length=64),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> PageResponse[WorkItemSummary]:
    rows, total = work_service.list_work_items(
        session,
        tenant_id=context.tenant_id,
        source_app=source_app,
        queue_code=queue_code,
        department_code=department_code,
        status=work_status,
        assigned_user_id=assigned_user_id,
        assigned_to_me=context.user_id if assigned_to_me else None,
        unassigned=unassigned,
        priority=priority,
        sla_state=sla_state,
        external_ref=external_ref,
        search=search,
        overdue_only=overdue_only,
        due_today=due_today,
        locked_by_me=context.user_id if locked_by_me else None,
        locked_by=context.user_id if locked else None,
        created_from=created_from,
        created_to=created_to,
        due_before=due_before,
        due_after=due_after,
        age_min_hours=age_min_hours,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    return PageResponse(
        data=[_summary(row, context.user_id) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.get(
    "/search",
    response_model=SearchResponse,
    responses={403: {"model": ErrorResponse}},
    summary="Global case search across references, names and queues",
)
def search_work_items(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    q: str = Query(min_length=2, max_length=150),
    limit: int = Query(default=25, ge=1, le=100),
) -> SearchResponse:
    rows, total = work_service.search_work_items(
        session, tenant_id=context.tenant_id, query=q, limit=limit
    )
    return SearchResponse(
        query=q,
        total=total,
        results=[
            SearchResultItem(
                work_item_id=row.id,
                source_app=row.source_app,
                external_ref=row.external_ref,
                title=row.title,
                customer_name=row.customer_name,
                customer_mobile_masked=row.customer_mobile_masked,
                queue_code=row.queue_code,
                status=row.status,
                assigned_user_name=row.assigned_user_name,
                deep_link=row.deep_link,
            )
            for row in rows
        ],
    )


@router.get(
    "/{work_item_id}",
    response_model=WorkItemDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Work item detail",
)
def get_work_item(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> WorkItemDetail:
    item = work_service.get_work_item(
        session, tenant_id=context.tenant_id, work_item_id=work_item_id
    )
    return _detail(session, item, context.user_id)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@router.post(
    "",
    response_model=WorkItemDetail,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Create a work item",
)
def create_work_item(
    payload: WorkItemCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanCreate)],
) -> WorkItemDetail:
    item = work_service.create_work_item(
        session, tenant_id=context.tenant_id, data=payload.model_dump(), ctx=_ctx(context)
    )
    if payload.auto_assign:
        # A savepoint means "nobody eligible" rolls back only the allocation,
        # never the created work item - the dashboard surfaces it as unassigned.
        savepoint = session.begin_nested()
        try:
            work_service.auto_assign(
                session, tenant_id=context.tenant_id, work_item_id=item.id, ctx=_ctx(context)
            )
            savepoint.commit()
        except Exception:
            savepoint.rollback()
    session.commit()
    return _detail(session, item, context.user_id)


# --------------------------------------------------------------------------
# assignment
# --------------------------------------------------------------------------
@router.get(
    "/{work_item_id}/candidates",
    response_model=EligibilityResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Eligibility breakdown for the work item's queue",
)
def candidates(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> EligibilityResponse:
    """Explains *why* somebody is or is not assignable - the manager view."""
    item = work_service.get_work_item(
        session, tenant_id=context.tenant_id, work_item_id=work_item_id
    )
    _queue, results = work_service.evaluate_queue(
        session, tenant_id=context.tenant_id, queue_code=item.queue_code
    )
    return EligibilityResponse(
        queue_code=item.queue_code,
        candidates=[
            CandidateItem(
                user_id=result.candidate.user_id,
                display_name=result.candidate.display_name,
                username=result.candidate.username,
                current_workload=result.candidate.open_workload,
                capacity=result.effective_capacity,
                priority_weight=result.candidate.membership.priority_weight,
                specialisation_match=result.specialisation_match,
                is_available=result.candidate.user.is_available
                and result.candidate.unavailable_until is None,
                daily_allocations_today=result.candidate.daily_allocations_today,
                eligible=result.eligible,
                ineligible_reasons=list(result.reasons),
            )
            for result in results
        ],
        eligible_count=sum(1 for result in results if result.eligible),
        checked_at=datetime.now(UTC),
    )


@router.post(
    "/{work_item_id}/auto-assign",
    response_model=AutoAssignResponse,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Allocate the work item to the best eligible user",
)
def auto_assign(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanAssign)],
) -> AutoAssignResponse:
    """Returns 409 with actionable reasons when nobody is eligible (PRD 17.3)."""
    item, chosen, results = work_service.auto_assign(
        session, tenant_id=context.tenant_id, work_item_id=work_item_id, ctx=_ctx(context)
    )
    session.commit()
    return AutoAssignResponse(
        work_item=_detail(session, item, context.user_id),
        assignee_user_id=chosen.candidate.user_id,
        assignee_name=chosen.candidate.display_name,
        explanation=item.allocation_explanation or "",
        candidates_considered=len(results),
    )


@router.post(
    "/{work_item_id}/assign",
    response_model=ManualAssignResponse,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Assign the work item to a specific user",
)
def assign(
    work_item_id: uuid.UUID,
    payload: AssignRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanAssign)],
) -> ManualAssignResponse:
    item = work_service.assign_manually(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        user_id=payload.user_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return ManualAssignResponse(
        work_item=_detail(session, item, context.user_id),
        explanation=(
            f"Assigned to {item.assigned_user_name} by {context.display_name}: " f"{payload.reason}"
        ),
    )


@router.post(
    "/{work_item_id}/unassign",
    response_model=WorkItemDetail,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Return the work item to the unassigned pool",
)
def unassign(
    work_item_id: uuid.UUID,
    payload: UnassignRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanAssign)],
) -> WorkItemDetail:
    item = work_service.unassign(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------
@router.post(
    "/{work_item_id}/lock",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Claim a work item",
)
def lock(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanLock)],
) -> WorkItemDetail:
    """Two concurrent claims produce exactly one 200 and one 409 (PRD 18)."""
    item = work_service.acquire_lock(
        session, tenant_id=context.tenant_id, work_item_id=work_item_id, ctx=_ctx(context)
    )
    session.commit()
    return _detail(session, item, context.user_id)


@router.post(
    "/{work_item_id}/heartbeat",
    response_model=HeartbeatResponse,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Extend the lock held by the caller",
)
def heartbeat(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanLock)],
) -> HeartbeatResponse:
    item = work_service.heartbeat(
        session, tenant_id=context.tenant_id, work_item_id=work_item_id, ctx=_ctx(context)
    )
    session.commit()
    remaining = (
        int((item.locked_until - datetime.now(UTC)).total_seconds()) if item.locked_until else 0
    )
    return HeartbeatResponse(
        work_item_id=item.id,
        locked_until=item.locked_until,
        seconds_remaining=remaining,
        lock_count=item.lock_count,
        last_heartbeat_at=item.last_heartbeat_at,
    )


@router.post(
    "/{work_item_id}/release",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Release a lock",
)
def release(
    work_item_id: uuid.UUID,
    payload: ReleaseLockRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanLock)],
) -> WorkItemDetail:
    item = work_service.release_lock(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


# --------------------------------------------------------------------------
# transfer / status / close
# --------------------------------------------------------------------------
@router.post(
    "/{work_item_id}/transfer",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    summary="Transfer work to another queue and/or user",
)
def transfer(
    work_item_id: uuid.UUID,
    payload: TransferRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanTransfer)],
) -> WorkItemDetail:
    """The reason is mandatory and the API rejects an empty one (PRD 20)."""
    if not payload.reason or not payload.reason.strip():
        raise ValidationError(
            "Give a reason for the transfer so the next person understands why.",
            code="REASON_REQUIRED",
        )
    item = work_service.transfer(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        destination_queue_code=payload.queue_code,
        destination_user_id=payload.user_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


@router.post(
    "/{work_item_id}/status",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Move a work item through its lifecycle",
)
def change_status(
    work_item_id: uuid.UUID,
    payload: StatusChangeRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanAssign)],
) -> WorkItemDetail:
    item = work_service.change_status(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        new_status=payload.status,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


@router.post(
    "/{work_item_id}/close",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Close a work item and release its lock",
)
def close_work_item(
    work_item_id: uuid.UUID,
    payload: CloseRequest,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanClose)],
) -> WorkItemDetail:
    item = work_service.close_work_item(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        reason=payload.reason,
        outcome=payload.outcome,
        notes=payload.notes,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


@router.post(
    "/{work_item_id}/due-at",
    response_model=WorkItemDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Override the SLA deadline for a work item",
)
def override_due_at(
    work_item_id: uuid.UUID,
    payload: DueAtOverride,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanAssign)],
) -> WorkItemDetail:
    item = work_service.override_due_at(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        due_at=payload.due_at,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, item, context.user_id)


# --------------------------------------------------------------------------
# timeline
# --------------------------------------------------------------------------
@router.get(
    "/{work_item_id}/events",
    response_model=PageResponse[WorkItemEventItem],
    responses={404: {"model": ErrorResponse}},
    summary="Case timeline",
)
def list_events(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> PageResponse[WorkItemEventItem]:
    work_service.get_work_item(session, tenant_id=context.tenant_id, work_item_id=work_item_id)
    rows, total = work_service.list_events(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=[WorkItemEventItem.model_validate(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


# --------------------------------------------------------------------------
# comments
# --------------------------------------------------------------------------
def _comment_item(row: CaseComment, *, can_edit: bool, can_delete: bool) -> CommentItem:
    return CommentItem(
        id=row.id,
        work_item_id=row.work_item_id,
        author_user_id=row.author_user_id,
        author_name=row.author_name,
        comment="" if row.is_deleted else row.comment,
        created_at=row.created_at,
        updated_at=row.updated_at,
        edit_count=row.edit_count,
        is_deleted=row.is_deleted,
        deleted_at=row.deleted_at,
        can_edit=can_edit,
        can_delete=can_delete,
    )


@router.get(
    "/{work_item_id}/comments",
    response_model=PageResponse[CommentItem],
    responses={404: {"model": ErrorResponse}},
    summary="Case comments",
)
def list_comments(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    include_deleted: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> PageResponse[CommentItem]:
    work_service.get_work_item(session, tenant_id=context.tenant_id, work_item_id=work_item_id)
    rows, total = work_service.list_comments(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        include_deleted=include_deleted,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=[
            _comment_item(
                row,
                can_edit=row.author_user_id == context.user_id and not row.is_deleted,
                can_delete=context.has(Permission.OPS_ROLES_WRITE) and not row.is_deleted,
            )
            for row in rows
        ],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.post(
    "/{work_item_id}/comments",
    response_model=CommentItem,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Add a comment",
)
def add_comment(
    work_item_id: uuid.UUID,
    payload: CommentCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanComment)],
) -> CommentItem:
    row = work_service.add_comment(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        comment=payload.comment,
        ctx=_ctx(context),
    )
    session.commit()
    return _comment_item(row, can_edit=True, can_delete=context.has(Permission.OPS_ROLES_WRITE))


@router.put(
    "/{work_item_id}/comments/{comment_id}",
    response_model=CommentItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Edit your own comment (original preserved in audit history)",
)
def edit_comment(
    work_item_id: uuid.UUID,
    comment_id: uuid.UUID,
    payload: CommentUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanComment)],
) -> CommentItem:
    work_service.get_work_item(session, tenant_id=context.tenant_id, work_item_id=work_item_id)
    row = work_service.edit_comment(
        session,
        tenant_id=context.tenant_id,
        comment_id=comment_id,
        comment=payload.comment,
        edit_reason=payload.edit_reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _comment_item(row, can_edit=False, can_delete=context.has(Permission.OPS_ROLES_WRITE))


@router.delete(
    "/{work_item_id}/comments/{comment_id}",
    response_model=CommentItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Soft delete a comment (privileged, audited)",
)
def delete_comment(
    work_item_id: uuid.UUID,
    comment_id: uuid.UUID,
    payload: CommentDelete,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanComment)],
) -> CommentItem:
    work_service.get_work_item(session, tenant_id=context.tenant_id, work_item_id=work_item_id)
    row = work_service.delete_comment(
        session,
        tenant_id=context.tenant_id,
        comment_id=comment_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _comment_item(row, can_edit=False, can_delete=False)
