"""User management routes (PRD 38).

Reads require ``OPS_USERS_READ``; every mutation requires ``OPS_USERS_WRITE``.
There is no password endpoint here and there never will be one - BRE owns
credentials (PRD Rule 1).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.models.user import OperationalStatus, User
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.org import (
    UserCreate,
    UserDetail,
    UserListItem,
    UserRoleAssignment,
    UserStatusChange,
    UserSummary,
    UserUpdate,
)
from app.security.permissions import ALL_PERMISSIONS, Permission, permissions_for_roles
from app.services import users as user_service
from app.services.org import WriteContext

router = APIRouter(prefix="/users", tags=["users"])

CanRead = require_permissions(Permission.OPS_USERS_READ)
CanWrite = require_permissions(Permission.OPS_USERS_WRITE)


def _write_ctx(context: CurrentUserCtx) -> WriteContext:
    return WriteContext(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        permissions=context.permissions,
    )


def _user_detail(session: Session, tenant_id: str, user_id: uuid.UUID) -> UserDetail:
    """Shared detail projection used by every user endpoint."""
    user = user_service.get_user(session, tenant_id=tenant_id, user_id=user_id)
    workload = user_service.open_workload_counts(
        session, tenant_id=tenant_id, user_ids=[user.id]
    ).get(user.id, 0)
    locked = user_service.locked_case_counts(session, tenant_id=tenant_id, user_ids=[user.id]).get(
        user.id, 0
    )
    unavailable = user_service.active_unavailability(session, tenant_id=tenant_id).get(user.id)
    departments = user_service.department_codes_by_user(
        session, tenant_id=tenant_id, user_ids=[user.id]
    ).get(user.id, [])
    queues = user_service.queue_codes_by_user(session, tenant_id=tenant_id, user_ids=[user.id]).get(
        user.id, []
    )

    return UserDetail(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        status=user.status,
        is_available=user.is_available,
        capacity=user.capacity,
        priority_weight=user.priority_weight,
        tenant_id=user.tenant_id,
        job_title=user.job_title,
        roles=user.role_keys,
        bre_roles=[role for role in (user.bre_roles or "").split(",") if role],
        open_workload=workload,
        department_codes=sorted(departments),
        queue_codes=sorted(queues),
        current_availability=unavailable.availability_type.value if unavailable else None,
        phone_masked=user.phone_masked,
        specialisations=user.specialisation_list,
        daily_allocation_limit=user.daily_allocation_limit,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        permissions=sorted(permissions_for_roles(user.role_keys)),
        locked_case_count=locked,
        available_now=not unavailable and user.is_available,
        unavailable_until=unavailable.end_at if unavailable else user.availability_ends_at,
    )


def _user_list_item(session: Session, tenant_id: str, user: User) -> UserListItem:
    workload = user_service.open_workload_counts(
        session, tenant_id=tenant_id, user_ids=[user.id]
    ).get(user.id, 0)
    departments = user_service.department_codes_by_user(
        session, tenant_id=tenant_id, user_ids=[user.id]
    ).get(user.id, [])
    queues = user_service.queue_codes_by_user(session, tenant_id=tenant_id, user_ids=[user.id]).get(
        user.id, []
    )
    unavailable = user_service.active_unavailability(session, tenant_id=tenant_id).get(user.id)

    return UserListItem(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        status=user.status,
        is_available=user.is_available,
        capacity=user.capacity,
        priority_weight=user.priority_weight,
        tenant_id=user.tenant_id,
        job_title=user.job_title,
        roles=user.role_keys,
        bre_roles=[role for role in (user.bre_roles or "").split(",") if role],
        open_workload=workload,
        department_codes=sorted(departments),
        queue_codes=sorted(queues),
        current_availability=unavailable.availability_type.value if unavailable else None,
    )


@router.get(
    "",
    response_model=PageResponse[UserListItem],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="List operational users",
)
def list_users(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    search: str | None = Query(default=None, max_length=150),
    user_status: OperationalStatus | None = Query(default=None, alias="status"),
    department_id: uuid.UUID | None = None,
    queue_id: uuid.UUID | None = None,
    is_available: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="display_name", max_length=64),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> PageResponse[UserListItem]:
    rows, total = user_service.list_users(
        session,
        tenant_id=context.tenant_id,
        search=search,
        status=user_status,
        department_id=department_id,
        queue_id=queue_id,
        is_available=is_available,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort=sort,
        descending=order == "desc",
    )
    return PageResponse(
        data=[_user_list_item(session, context.tenant_id, user) for user in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )


@router.get(
    "/meta/roles",
    response_model=list[str],
    summary="Valid operational role names",
)
def list_roles(
    context: CurrentUserCtx, _: Annotated[RequestContext, Depends(CanRead)]
) -> list[str]:
    from app.services.users import VALID_ROLES

    return sorted(VALID_ROLES)


@router.get(
    "/meta/permissions",
    response_model=list[str],
    summary="Every permission the API recognises",
)
def list_permissions(
    context: CurrentUserCtx, _: Annotated[RequestContext, Depends(CanRead)]
) -> list[str]:
    """Feeds the permission matrix in the admin UI and the drift test."""
    return sorted(ALL_PERMISSIONS)


@router.get(
    "/{user_id}",
    response_model=UserDetail,
    responses={404: {"model": ErrorResponse}},
    summary="User detail with workload and recent activity",
)
def get_user(
    user_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> UserDetail:
    return _user_detail(session, context.tenant_id, user_id)


@router.post(
    "",
    response_model=UserDetail,
    status_code=status.HTTP_201_CREATED,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Create an operational user projection",
)
def create_user(
    payload: UserCreate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> UserDetail:
    user = user_service.create_user(
        session,
        tenant_id=context.tenant_id,
        data=payload,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    session.commit()
    return _user_detail(session, context.tenant_id, user.id)


@router.put(
    "/{user_id}",
    response_model=UserDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Update operational user metadata",
)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> UserDetail:
    user = user_service.update_user(
        session,
        tenant_id=context.tenant_id,
        user_id=user_id,
        data=payload,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    session.commit()
    return _user_detail(session, context.tenant_id, user.id)


@router.post(
    "/{user_id}/activate",
    response_model=UserDetail,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Activate a deactivated user",
)
def activate_user(
    user_id: uuid.UUID,
    payload: UserStatusChange,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> UserDetail:
    user = user_service.set_user_status(
        session,
        tenant_id=context.tenant_id,
        user_id=user_id,
        status=OperationalStatus.ACTIVE,
        reason=payload.reason,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    session.commit()
    return _user_detail(session, context.tenant_id, user.id)


@router.post(
    "/{user_id}/deactivate",
    response_model=UserDetail,
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
    summary="Deactivate a user",
)
def deactivate_user(
    user_id: uuid.UUID,
    payload: UserStatusChange,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> UserDetail:
    user = user_service.set_user_status(
        session,
        tenant_id=context.tenant_id,
        user_id=user_id,
        status=OperationalStatus.INACTIVE,
        reason=payload.reason,
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    session.commit()
    return _user_detail(session, context.tenant_id, user.id)


@router.put(
    "/{user_id}/roles",
    response_model=UserSummary,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Replace the operational roles held by a user",
)
def assign_roles(
    user_id: uuid.UUID,
    payload: UserRoleAssignment,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanWrite)],
) -> UserSummary:
    """Role changes are audited with before/after so drift stays traceable."""
    user = user_service.assign_roles(
        session,
        tenant_id=context.tenant_id,
        user_id=user_id,
        roles=payload.roles,
        reason="Operational roles reassigned",
        actor_user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    session.commit()
    return UserSummary.model_validate(user)


@router.get(
    "/{user_id}/recent-activity",
    response_model=list[dict[str, Any]],
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Recent operational actions performed by a user",
)
def recent_activity(
    user_id: uuid.UUID,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    from app.models.audit import AuditLog, EntityType

    user_service.get_user(session, tenant_id=context.tenant_id, user_id=user_id)
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == context.tenant_id,
            AuditLog.actor_user_id == user_id,
            AuditLog.entity_type != EntityType.AUTH,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "action": row.action.value,
            "entity_type": row.entity_type.value,
            "entity_id": row.entity_id,
            "reason": row.reason,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
