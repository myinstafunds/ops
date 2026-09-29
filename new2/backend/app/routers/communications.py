"""Communication log routes (PRD 26, 38).

Responses carry ``customer_mobile_masked`` only. The stored number is never
serialised, so a UI bug cannot leak it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.communication import (
    Channel,
    CommunicationEvent,
    CommunicationStatus,
    Direction,
)
from app.models.work import WorkItem
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.work import CommunicationCreate, CommunicationItem
from app.security.permissions import Permission
from app.services import communications as communication_service
from app.services.org import WriteContext

router = APIRouter(prefix="/communications", tags=["communications"])

CanRead = require_permissions(Permission.OPS_COMMUNICATION_READ)
CanWrite = require_permissions(Permission.OPS_COMMUNICATION_WRITE)


def _ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


def _item(row: CommunicationEvent) -> CommunicationItem:
    return CommunicationItem(
        id=row.id,
        work_item_id=row.work_item_id,
        work_item_ref=row.work_item.external_ref if row.work_item else None,
        channel=row.channel,
        direction=row.direction,
        status=row.status,
        customer_reference=row.customer_reference,
        customer_name=row.customer_name,
        customer_mobile_masked=row.mobile_masked,
        actor_name=row.actor_name,
        subject=row.subject,
        body_preview=row.body_preview,
        outcome_notes=row.outcome_notes,
        disposition=row.disposition,
        provider_message_id=row.provider_message_id,
        retry_count=row.retry_count or 0,
        occurred_at=row.occurred_at,
        delivered_at=row.delivered_at,
        duration_seconds=row.duration_seconds,
        created_at=row.created_at,
    )


@router.get(
    "",
    response_model=PageResponse[CommunicationItem],
    responses={403: {"model": ErrorResponse}},
    summary="List customer communications",
)
def list_communications(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    work_item_id: uuid.UUID | None = None,
    channel: Channel | None = None,
    direction: Direction | None = None,
    communication_status: CommunicationStatus | None = Query(default=None, alias="status"),
    actor_user_id: uuid.UUID | None = None,
    customer_reference: str | None = Query(default=None, max_length=128),
    search: str | None = Query(default=None, max_length=150),
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="occurred_at", max_length=64),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> PageResponse[CommunicationItem]:
    rows, total = communication_service.list_communications(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        channel=channel,
        direction=direction,
        status=communication_status,
        actor_user_id=actor_user_id,
        customer_reference=customer_reference,
        search=search,
        occurred_from=from_at,
        occurred_to=to_at,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    return PageResponse(
        data=[_item(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.post(
    "",
    response_model=CommunicationItem,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    summary="Log a customer communication",
)
def create_communication(
    payload: CommunicationCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> CommunicationItem:
    row = communication_service.create_communication(
        session, tenant_id=context.tenant_id, data=payload.model_dump(), ctx=_ctx(context)
    )
    session.commit()
    return _item(row)


@router.get(
    "/for-case/{work_item_id}",
    response_model=PageResponse[CommunicationItem],
    responses={404: {"model": ErrorResponse}},
    summary="Communications linked to one case",
)
def communications_for_case(
    work_item_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PageResponse[CommunicationItem]:
    from sqlalchemy import select

    item = session.scalar(
        select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.tenant_id == context.tenant_id)
    )
    if item is None:
        from app.errors import NotFoundError

        raise NotFoundError("Work item was not found.")

    rows, total = communication_service.list_communications(
        session,
        tenant_id=context.tenant_id,
        work_item_id=work_item_id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=[_item(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )
