"""Department routes (PRD 12, 38)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.org import DepartmentMembership, OrgStatus, SourceApp
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.org import (
    DepartmentCreate,
    DepartmentDetail,
    DepartmentMembershipCreate,
    DepartmentMembershipItem,
    DepartmentMembershipUpdate,
    DepartmentSummary,
    DepartmentUpdate,
)
from app.security.permissions import Permission
from app.services import org as org_service
from app.services.org import WriteContext

router = APIRouter(prefix="/departments", tags=["departments"])

CanRead = require_permissions(Permission.OPS_DEPARTMENTS_READ)
CanWrite = require_permissions(Permission.OPS_DEPARTMENTS_WRITE)


def _ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


@router.get(
    "",
    response_model=PageResponse[DepartmentSummary],
    responses={403: {"model": ErrorResponse}},
    summary="List departments",
)
def list_departments(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    search: str | None = Query(default=None, max_length=150),
    org_status: OrgStatus | None = Query(default=None, alias="status"),
    source_app: SourceApp | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="name", max_length=64),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> PageResponse[DepartmentSummary]:
    rows, total, member_counts = org_service.list_departments(
        session,
        tenant_id=context.tenant_id,
        search=search,
        status=org_status,
        source_app=source_app,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    return PageResponse(
        data=[
            DepartmentSummary(
                id=row.id,
                code=row.code,
                name=row.name,
                source_app=row.source_app,
                status=row.status,
                member_count=member_counts.get(row.id, 0),
            )
            for row in rows
        ],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.get(
    "/{department_id}",
    response_model=DepartmentDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Department detail",
)
def get_department(
    department_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> DepartmentDetail:
    return _detail(session, context.tenant_id, department_id)


def _detail(session: Session, tenant_id: str, department_id: uuid.UUID) -> DepartmentDetail:
    """Load a department and shape the detail payload.

    Shared by the GET/POST/PUT handlers so all three answer identically.
    """
    department = org_service.get_department(
        session, tenant_id=tenant_id, department_id=department_id
    )
    return DepartmentDetail(
        id=department.id,
        code=department.code,
        name=department.name,
        source_app=department.source_app,
        status=department.status,
        member_count=org_service.department_member_count(
            session, tenant_id=tenant_id, department_id=department.id
        ),
        description=department.description,
        created_at=department.created_at,
        updated_at=department.updated_at,
    )


@router.post(
    "",
    response_model=DepartmentDetail,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Create a department",
)
def create_department(
    payload: DepartmentCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> DepartmentDetail:
    department = org_service.create_department(session, data=payload, ctx=_ctx(context))
    session.commit()
    return _detail(session, context.tenant_id, department.id)


@router.put(
    "/{department_id}",
    response_model=DepartmentDetail,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Update a department",
)
def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> DepartmentDetail:
    department = org_service.update_department(
        session,
        tenant_id=context.tenant_id,
        department_id=department_id,
        data=payload,
        ctx=_ctx(context),
    )
    session.commit()
    return _detail(session, context.tenant_id, department.id)


# --------------------------------------------------------------------------
# memberships
# --------------------------------------------------------------------------
memberships_router = APIRouter(prefix="/department-memberships", tags=["department-memberships"])


@memberships_router.get(
    "",
    response_model=PageResponse[DepartmentMembershipItem],
    responses={403: {"model": ErrorResponse}},
    summary="List department memberships",
)
def list_department_memberships(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    department_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PageResponse[DepartmentMembershipItem]:
    rows, total = org_service.list_department_memberships(
        session,
        tenant_id=context.tenant_id,
        department_id=department_id,
        user_id=user_id,
        is_active=is_active,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PageResponse(
        data=[_membership_item(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


def _membership_item(row: DepartmentMembership) -> DepartmentMembershipItem:
    return DepartmentMembershipItem(
        id=row.id,
        department_id=row.department_id,
        department_code=row.department.code,
        department_name=row.department.name,
        user_id=row.user_id,
        user_name=row.user.display_name,
        username=row.user.username,
        is_primary=row.is_primary,
        is_active=row.is_active,
        created_at=row.created_at,
    )


@memberships_router.post(
    "",
    response_model=DepartmentMembershipItem,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Add a user to a department",
)
def create_department_membership(
    payload: DepartmentMembershipCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> DepartmentMembershipItem:
    row = org_service.upsert_department_membership(
        session,
        department_id=payload.department_id,
        user_id=payload.user_id,
        is_primary=payload.is_primary,
        ctx=_ctx(context),
    )
    session.commit()
    return _membership_item(row)


@memberships_router.patch(
    "/{membership_id}",
    response_model=DepartmentMembershipItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Update a department membership",
)
def update_department_membership(
    membership_id: uuid.UUID,
    payload: DepartmentMembershipUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> DepartmentMembershipItem:
    row = org_service.update_department_membership(
        session,
        tenant_id=context.tenant_id,
        membership_id=membership_id,
        is_primary=payload.is_primary,
        is_active=payload.is_active,
        ctx=_ctx(context),
    )
    session.commit()
    return _membership_item(row)
