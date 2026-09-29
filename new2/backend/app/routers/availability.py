"""Availability routes (PRD 14, 38).

Approval is separated from creation on purpose: ``OPS_AVAILABILITY_WRITE`` lets
you raise a request, ``OPS_AVAILABILITY_APPROVE`` lets you decide one, and the
service refuses to let the same person do both.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.availability import (
    AvailabilityRequestStatus,
    AvailabilityType,
    UserAvailability,
)
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.org import (
    AvailabilityCreate,
    AvailabilityCreateForSelf,
    AvailabilityDecision,
    AvailabilityItem,
    AvailabilityUpdate,
)
from app.security.permissions import Permission
from app.services import availability as availability_service
from app.services.org import WriteContext

router = APIRouter(prefix="/availability", tags=["availability"])

CanRead = require_permissions(Permission.OPS_AVAILABILITY_READ)
CanWrite = require_permissions(Permission.OPS_AVAILABILITY_WRITE)
CanApprove = require_permissions(Permission.OPS_AVAILABILITY_APPROVE)


def _ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


def _item(row: UserAvailability, moment: datetime) -> AvailabilityItem:
    return AvailabilityItem(
        id=row.id,
        user_id=row.user_id,
        user_name=row.user.display_name if row.user else "",
        username=row.user.username if row.user else "",
        availability_type=row.availability_type,
        status=row.status,
        start_at=row.start_at,
        end_at=row.end_at,
        reason=row.reason,
        notes=row.notes,
        created_by_name=row.created_by_name,
        created_at=row.created_at,
        decided_by_name=row.decided_by_name,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        is_active_window=row.start_at <= moment < row.end_at
        and row.status is AvailabilityRequestStatus.APPROVED,
        blocks_allocation=row.blocks_allocation,
    )


@router.get(
    "",
    response_model=PageResponse[AvailabilityItem],
    responses={403: {"model": ErrorResponse}},
    summary="List availability windows",
)
def list_availability(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    user_id: uuid.UUID | None = None,
    request_status: AvailabilityRequestStatus | None = Query(default=None, alias="status"),
    availability_type: AvailabilityType | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    active_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="start_at", max_length=64),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> PageResponse[AvailabilityItem]:
    rows, total = availability_service.list_availability(
        session,
        tenant_id=context.tenant_id,
        user_id=user_id,
        status=request_status,
        availability_type=availability_type,
        from_at=from_at,
        to_at=to_at,
        active_only=active_only,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    moment = datetime.now(UTC)
    return PageResponse(
        data=[_item(row, moment) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.post(
    "",
    response_model=AvailabilityItem,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Raise an availability request for a user",
)
def create_availability(
    payload: AvailabilityCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> AvailabilityItem:
    row = availability_service.create_availability(
        session, tenant_id=context.tenant_id, data=payload, ctx=_ctx(context)
    )
    session.commit()
    return _item(row, datetime.now(UTC))


@router.post(
    "/self",
    response_model=AvailabilityItem,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Raise an availability request for yourself",
)
def create_availability_for_self(
    payload: AvailabilityCreateForSelf,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> AvailabilityItem:
    """Always starts in REQUESTED - self approval is impossible (PRD 14)."""
    data = AvailabilityCreate(
        user_id=context.user_id,
        availability_type=payload.availability_type,
        start_at=payload.start_at,
        end_at=payload.end_at,
        reason=payload.reason,
        notes=payload.notes,
    )
    row = availability_service.create_availability(
        session, tenant_id=context.tenant_id, data=data, ctx=_ctx(context)
    )
    session.commit()
    return _item(row, datetime.now(UTC))


@router.get(
    "/{availability_id}",
    response_model=AvailabilityItem,
    responses={404: {"model": ErrorResponse}},
    summary="Availability request detail",
)
def get_availability(
    availability_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> AvailabilityItem:
    row = availability_service.get_availability(
        session, tenant_id=context.tenant_id, availability_id=availability_id
    )
    return _item(row, datetime.now(UTC))


@router.patch(
    "/{availability_id}",
    response_model=AvailabilityItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Edit an availability request",
)
def update_availability(
    availability_id: uuid.UUID,
    payload: AvailabilityUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> AvailabilityItem:
    row = availability_service.update_availability(
        session,
        tenant_id=context.tenant_id,
        availability_id=availability_id,
        data=payload,
        ctx=_ctx(context),
    )
    session.commit()
    return _item(row, datetime.now(UTC))


@router.post(
    "/{availability_id}/approve",
    response_model=AvailabilityItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Approve an availability request",
)
def approve_availability(
    availability_id: uuid.UUID,
    payload: AvailabilityDecision,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanApprove)],
) -> AvailabilityItem:
    row = availability_service.approve_availability(
        session,
        tenant_id=context.tenant_id,
        availability_id=availability_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _item(row, datetime.now(UTC))


@router.post(
    "/{availability_id}/reject",
    response_model=AvailabilityItem,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Reject an availability request",
)
def reject_availability(
    availability_id: uuid.UUID,
    payload: AvailabilityDecision,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanApprove)],
) -> AvailabilityItem:
    row = availability_service.reject_availability(
        session,
        tenant_id=context.tenant_id,
        availability_id=availability_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _item(row, datetime.now(UTC))


@router.post(
    "/{availability_id}/cancel",
    response_model=AvailabilityItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Cancel an availability request",
)
def cancel_availability(
    availability_id: uuid.UUID,
    payload: AvailabilityDecision,
    context: CurrentUserCtx,
    session: DbSession,
    #: Cancelling mutates state, so it needs the write guard - a read
    #: permission would let anyone withdraw anybody's request.
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> AvailabilityItem:
    row = availability_service.cancel_availability(
        session,
        tenant_id=context.tenant_id,
        availability_id=availability_id,
        reason=payload.reason,
        ctx=_ctx(context),
    )
    session.commit()
    return _item(row, datetime.now(UTC))
