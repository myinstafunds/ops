"""Authentication routes (PRD 38).

``POST /api/auth/login``, ``/mfa/verify``, ``/logout``, ``POST /api/auth/refresh``
and ``GET /api/auth/me``. Everything except login and MFA requires a token.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, status

from app.dependencies.auth import CurrentUserCtx, DbSession
from app.errors import AuthenticationError
from app.schemas.common import ErrorResponse, MessageResponse
from app.schemas.org import (
    CurrentUser,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    MfaVerifyRequest,
)
from app.security.permissions import permissions_for_roles
from app.services import auth as auth_service
from app.services.users import department_codes_by_user, queue_codes_by_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    summary="Sign in with BRE Identity credentials",
)
def login(payload: LoginRequest, request: Request, session: DbSession) -> LoginResponse:
    outcome = auth_service.login(
        session,
        username=payload.username,
        password=payload.password,
        tenant_id=payload.tenant_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if outcome.mfa_required:
        return LoginResponse(mfa_required=True, mfa_token=outcome.mfa_token)
    return LoginResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=_expires_in(outcome.expires_at),
        mfa_required=False,
    )


@router.post(
    "/mfa/verify",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Complete multi-factor verification",
)
def verify_mfa(payload: MfaVerifyRequest, request: Request, session: DbSession) -> LoginResponse:
    outcome = auth_service.verify_mfa(
        session,
        mfa_token=payload.mfa_token,
        code=payload.code,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return LoginResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=_expires_in(outcome.expires_at),
        mfa_required=False,
    )


@router.post(
    "/refresh",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Exchange a refresh token for a new session",
)
def refresh(payload: LogoutRequest, request: Request, session: DbSession) -> LoginResponse:
    if not payload.refresh_token:
        raise AuthenticationError("A refresh token is required.")
    outcome = auth_service.refresh(
        session,
        refresh_token=payload.refresh_token,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return LoginResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=_expires_in(outcome.expires_at),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    responses={401: {"model": ErrorResponse}},
    summary="End the current session",
)
def logout(
    payload: LogoutRequest,
    request: Request,
    session: DbSession,
    context: CurrentUserCtx,
) -> MessageResponse:
    auth_service.logout(
        session,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        actor_name=context.display_name,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return MessageResponse(message="You have been signed out.")


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="Current signed-in user with effective permissions",
)
def me(session: DbSession, context: CurrentUserCtx) -> CurrentUser:
    from sqlalchemy import select

    from app.models.user import User

    user = session.scalar(
        select(User).where(User.id == context.user_id, User.tenant_id == context.tenant_id)
    )
    if user is None:
        raise AuthenticationError("Your account is no longer available.")

    roles = user.role_keys
    user_ids = [user.id]
    return CurrentUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        tenant_id=user.tenant_id,
        status=user.status,
        is_available=user.is_available,
        capacity=user.capacity,
        bre_user_id=user.bre_user_id,
        bre_roles=[role for role in (user.bre_roles or "").split(",") if role],
        roles=roles,
        permissions=sorted(permissions_for_roles(roles)),
        departments=sorted(
            department_codes_by_user(session, tenant_id=context.tenant_id, user_ids=user_ids).get(
                user.id, []
            )
        ),
        queues=sorted(
            queue_codes_by_user(session, tenant_id=context.tenant_id, user_ids=user_ids).get(
                user.id, []
            )
        ),
    )


def _expires_in(expires_at: datetime | None) -> int | None:
    if expires_at is None:
        return None
    return max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
