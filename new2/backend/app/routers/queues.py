"""Queue routes (PRD 13, 38).

Two URL spaces live here:

* ``/api/queues``               - the queue register and its members
* ``/api/queue-memberships``    - the membership table itself, so the
  membership screen can filter across queues
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.org import OrgStatus, Queue, QueueMembership, SourceApp
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.org import (
    QueueCreate,
    QueueDetail,
    QueueMembershipCreate,
    QueueMembershipItem,
    QueueMembershipUpdate,
    QueueSummary,
    QueueUpdate,
)
from app.security.permissions import Permission
from app.services import availability as availability_service
from app.services import org as org_service
from app.services.org import WriteContext

router = APIRouter(prefix="/queues", tags=["queues"])
memberships_router = APIRouter(prefix="/queue-memberships", tags=["queue-memberships"])

CanRead = require_permissions(Permission.OPS_QUEUES_READ)
CanWrite = require_permissions(Permission.OPS_QUEUES_WRITE)


def _ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


def _summary(row: Queue, *, capacity: org_service.QueueCapacity) -> QueueSummary:
    return QueueSummary(
        id=row.id,
        code=row.code,
        name=row.name,
        source_app=row.source_app,
        status=row.status,
        priority=row.priority,
        max_workload_per_user=row.max_workload_per_user,
        open_items=capacity.open_items,
        available_members=capacity.available_members,
        member_count=capacity.member_count,
    )


def _detail(session: Session, tenant_id: str, row: Queue) -> QueueDetail:
    policy = org_service.queue_sla_policy(session, tenant_id=tenant_id, queue=row)
    capacity = org_service.queue_capacity(session, tenant_id=tenant_id, queue=row)
    return QueueDetail(
        id=row.id,
        code=row.code,
        name=row.name,
        source_app=row.source_app,
        status=row.status,
        priority=row.priority,
        max_workload_per_user=row.max_workload_per_user,
        open_items=capacity.open_items,
        available_members=capacity.available_members,
        member_count=capacity.member_count,
        description=row.description,
        department_code=row.department.code if row.department else None,
        eligible_roles=list(row.eligible_roles or []),
        required_specialisations=list(row.required_specialisations or []),
        sla_policy_id=row.sla_policy_id,
        sla_target_minutes=policy.target_minutes if policy else None,
        sla_warning_minutes=policy.warning_minutes if policy else None,
        sync_interval_minutes=row.sync_interval_minutes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _membership_items(
    session: Session, tenant_id: str, rows: Sequence[QueueMembership]
) -> list[QueueMembershipItem]:
    """Build membership rows with workload and live availability resolved once."""
    if not rows:
        return []
    user_ids = [row.user_id for row in rows]
    workloads = org_service.open_workloads(session, tenant_id=tenant_id, user_ids=user_ids)
    unavailable = availability_service.unavailable_user_ids(session, tenant_id=tenant_id)
    return [
        QueueMembershipItem(
            id=row.id,
            queue_id=row.queue_id,
            queue_code=row.queue.code,
            queue_name=row.queue.name,
            user_id=row.user_id,
            user_name=row.user.display_name,
            username=row.user.username,
            is_active=row.is_active,
            capacity=row.capacity,
            priority_weight=row.priority_weight,
            daily_max_allocations=row.daily_max_allocations,
            specialisation=row.specialisation,
            shift_start=row.shift_start.isoformat() if row.shift_start else None,
            shift_end=row.shift_end.isoformat() if row.shift_end else None,
            open_workload=workloads.get(row.user_id, 0),
            available_now=(
                row.is_active and row.user.is_available and row.user_id not in unavailable
            ),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# queue register
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=PageResponse[QueueSummary],
    responses={403: {"model": ErrorResponse}},
    summary="List queues",
)
def list_queues(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    search: str | None = Query(default=None, max_length=150),
    org_status: OrgStatus | None = Query(default=None, alias="status"),
    source_app: SourceApp | None = None,
    department_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="name", max_length=64),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> PageResponse[QueueSummary]:
    rows, total = org_service.list_queues(
        session,
        tenant_id=context.tenant_id,
        search=search,
        status=org_status,
        source_app=source_app,
        department_id=department_id,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    capacities = org_service.queue_capacities(session, tenant_id=context.tenant_id, queues=rows)
    return PageResponse(
        data=[_summary(row, capacity=capacities[row.id]) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.get(
    "/{queue_id}",
    response_model=QueueDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Queue detail",
)
def get_queue(
    queue_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> QueueDetail:
    queue = org_service.get_queue(session, tenant_id=context.tenant_id, queue_id=queue_id)
    return _detail(session, context.tenant_id, queue)


@router.post(
    "",
    response_model=QueueDetail,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Create a queue",
)
def create_queue(
    payload: QueueCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> QueueDetail:
    queue = org_service.create_queue(session, data=payload, ctx=_ctx(context))
    session.commit()
    return _detail(session, context.tenant_id, queue)


@router.put(
    "/{queue_id}",
    response_model=QueueDetail,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Update a queue",
)
def update_queue(
    queue_id: uuid.UUID,
    payload: QueueUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> QueueDetail:
    queue = org_service.update_queue(
        session,
        tenant_id=context.tenant_id,
        queue_id=queue_id,
        data=payload,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, context.tenant_id, queue)


@router.get(
    "/{queue_id}/members",
    response_model=PageResponse[QueueMembershipItem],
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="List members of a queue",
)
def list_queue_members(
    queue_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PageResponse[QueueMembershipItem]:
    org_service.get_queue(session, tenant_id=context.tenant_id, queue_id=queue_id)
    rows, total = org_service.list_queue_memberships(
        session,
        tenant_id=context.tenant_id,
        queue_id=queue_id,
        is_active=is_active,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=_membership_items(session, context.tenant_id, rows),
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


# --------------------------------------------------------------------------
# memberships
# --------------------------------------------------------------------------
@memberships_router.get(
    "",
    response_model=PageResponse[QueueMembershipItem],
    responses={403: {"model": ErrorResponse}},
    summary="List queue memberships",
)
def list_queue_memberships(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    queue_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PageResponse[QueueMembershipItem]:
    rows, total = org_service.list_queue_memberships(
        session,
        tenant_id=context.tenant_id,
        queue_id=queue_id,
        user_id=user_id,
        is_active=is_active,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=_membership_items(session, context.tenant_id, rows),
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@memberships_router.post(
    "",
    response_model=QueueMembershipItem,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Add a user to a queue",
)
def create_queue_membership(
    payload: QueueMembershipCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> QueueMembershipItem:
    row = org_service.add_queue_member(session, data=payload.model_dump(), ctx=_ctx(context))
    session.commit()
    return _membership_items(session, context.tenant_id, [row])[0]


@memberships_router.patch(
    "/{membership_id}",
    response_model=QueueMembershipItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Update a queue membership",
)
def update_queue_membership(
    membership_id: uuid.UUID,
    payload: QueueMembershipUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> QueueMembershipItem:
    row = org_service.update_queue_member(
        session,
        tenant_id=context.tenant_id,
        membership_id=membership_id,
        changes=payload.model_dump(exclude_unset=True),
        ctx=_ctx(context),
    )
    session.commit()
    return _membership_items(session, context.tenant_id, [row])[0]
