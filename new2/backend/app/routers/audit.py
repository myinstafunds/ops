"""Audit log routes (PRD 28, 38). Read only - the table is append-only."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.audit import AuditAction, EntityType
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.work import AuditLogItem
from app.security.permissions import Permission
from app.services import audit as audit_service

router = APIRouter(prefix="/audit-log", tags=["audit"])

CanRead = require_permissions(Permission.OPS_AUDIT_READ)


@router.get(
    "",
    response_model=PageResponse[AuditLogItem],
    responses={403: {"model": ErrorResponse}},
    summary="Search the audit trail",
)
def list_audit_log(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    actor_user_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    entity_type: EntityType | None = None,
    entity_id: str | None = Query(default=None, max_length=128),
    work_item_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    search: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="created_at", max_length=64),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> PageResponse[AuditLogItem]:
    rows, total = audit_service.list_audit_entries(
        session,
        tenant_id=context.tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        work_item_id=work_item_id,
        from_at=from_at,
        to_at=to_at,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    return PageResponse(
        data=[AuditLogItem.model_validate(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.get(
    "/{audit_id}",
    response_model=AuditLogItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Audit entry detail",
)
def get_audit_entry(
    audit_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> AuditLogItem:
    entry = audit_service.get_audit_entry(session, tenant_id=context.tenant_id, audit_id=audit_id)
    return AuditLogItem.model_validate(entry)


@router.get(
    "/meta/actions",
    response_model=list[str],
    responses={403: {"model": ErrorResponse}},
    summary="Every audited action name",
)
def list_actions(
    context: CurrentUserCtx, _: Annotated[RequestContext, Depends(CanRead)]
) -> list[str]:
    return [action.value for action in audit_service.available_actions()]


@router.get(
    "/meta/entity-types",
    response_model=list[str],
    responses={403: {"model": ErrorResponse}},
    summary="Every audited entity type",
)
def list_entity_types(
    context: CurrentUserCtx, _: Annotated[RequestContext, Depends(CanRead)]
) -> list[str]:
    return [entity.value for entity in audit_service.available_entity_types()]


@router.get(
    "/for-entity/{entity_type}/{entity_id}",
    response_model=list[AuditLogItem],
    responses={403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Full history for one entity",
)
def entity_history(
    entity_type: str,
    entity_id: str,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditLogItem]:
    from app.errors import ValidationError

    try:
        resolved = EntityType(entity_type.upper())
    except ValueError as exc:
        raise ValidationError(
            f"'{entity_type}' is not a known entity type.",
            details={"valid_entity_types": [item.value for item in EntityType]},
        ) from exc

    rows = audit_service.audit_trail_for_entity(
        session,
        tenant_id=context.tenant_id,
        entity_type=resolved,
        entity_id=entity_id,
        limit=limit,
    )
    return [AuditLogItem.model_validate(row) for row in rows]
