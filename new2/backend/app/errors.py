"""Uniform API error handling.

Every failure leaves the API in the shape described by PRD 36::

    {
      "error": {
        "code": "CASE_NOT_FOUND",
        "message": "Work item was not found.",
        "details": {}
      },
      "trace_id": "..."
    }

Messages are written for the operator reading the screen. Technical detail
goes to the log, never to the response body (PRD 34.3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode:
    """Stable machine readable error codes."""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    NO_ELIGIBLE_USER = "NO_ELIGIBLE_USER"
    CASE_LOCKED = "CASE_LOCKED"
    SELF_APPROVAL_NOT_ALLOWED = "SELF_APPROVAL_NOT_ALLOWED"
    REASON_REQUIRED = "REASON_REQUIRED"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """Base class for every deliberate, operator facing failure."""

    # Per-class defaults that ``__init__`` overrides per instance, so an
    # instance always carries a concrete message, code, status and headers.
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = ErrorCode.VALIDATION_ERROR
    message: str = "The request could not be processed."
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        self.headers = headers or self.headers
        super().__init__(self.message)


class AuthenticationError(ApiError):
    status_code: int = status.HTTP_401_UNAUTHORIZED
    code: str = ErrorCode.UNAUTHENTICATED
    message: str = "Sign in to continue."
    # Class level default, shadowed per instance in ``ApiError.__init__``.
    headers: dict[str, str] | None = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012


class TokenExpiredError(AuthenticationError):
    code = ErrorCode.TOKEN_EXPIRED
    message = "Your session has expired. Sign in again."


class TokenInvalidError(AuthenticationError):
    code = ErrorCode.TOKEN_INVALID
    message = "Your sign-in token is not valid. Sign in again."


class PermissionDeniedError(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.FORBIDDEN
    message = "You do not have permission to perform this action."


class NotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND
    message = "The requested record was not found."


class ConflictError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT
    message = "The record was modified by someone else. Reload and try again."


class ValidationError(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = ErrorCode.VALIDATION_ERROR
    message = "Some of the values supplied are not valid."


class RateLimitedError(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED
    message = "Too many attempts. Please wait a moment and try again."


class UpstreamUnavailableError(ApiError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    message = "An upstream service is unavailable. Try again shortly."


class UpstreamTimeoutError(ApiError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = ErrorCode.UPSTREAM_TIMEOUT
    message = "An upstream service took too long to respond."


class NoEligibleUserError(ApiError):
    """Actionable 'nobody to assign' response (PRD 17.3)."""

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.NO_ELIGIBLE_USER
    message = "No eligible user is currently available for this work."

    def __init__(
        self,
        reasons: list[str] | None = None,
        *,
        queue_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "reasons": reasons
            or [
                "all users are unavailable",
                "all users are at capacity",
                "no eligible queue members",
                "required permission missing",
                "shift ended",
            ],
        }
        if queue_code:
            payload["queue_code"] = queue_code
        payload.update(details or {})
        super().__init__(self.message, details=payload)


class CaseLockedError(ConflictError):
    code = ErrorCode.CASE_LOCKED

    def __init__(self, locked_by_name: str, locked_until_iso: str) -> None:
        super().__init__(
            f"This case is currently locked by {locked_by_name}. "
            "It becomes claimable when the lock expires or they release it.",
            details={"locked_by_name": locked_by_name, "locked_until": locked_until_iso},
        )


class ReasonRequiredError(ValidationError):
    code = ErrorCode.REASON_REQUIRED
    message = "A reason is required for this action."


class FeatureDisabledError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.FEATURE_DISABLED
    message = "This capability is currently disabled."


def trace_id_of(request: Request) -> str | None:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) else None


def _envelope(
    request: Request,
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "details": details or {}},
            "trace_id": trace_id_of(request),
        },
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler in one place."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        # 5xx is our fault, so log it with a stack trace. 4xx is not.
        if exc.status_code >= 500:
            logger.exception("api_error code=%s trace=%s", exc.code, trace_id_of(request))
        return _envelope(
            request,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
            422: ErrorCode.VALIDATION_ERROR,
            429: ErrorCode.RATE_LIMITED,
        }
        detail = (
            exc.detail if isinstance(exc.detail, str) else "The request could not be processed."
        )
        return _envelope(
            request,
            code=mapping.get(exc.status_code, "HTTP_ERROR"),
            message=detail,
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope(
            request,
            code=ErrorCode.VALIDATION_ERROR,
            message="Some of the values supplied are not valid.",
            details={"errors": _summarise_validation(exc.errors())},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _handle_db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("database_error trace=%s", trace_id_of(request))
        return _envelope(
            request,
            code="DATABASE_ERROR",
            message=(
                "The request could not be completed because of a data error. " "Try again shortly."
            ),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error trace=%s", trace_id_of(request), exc_info=exc)
        return _envelope(
            request,
            code=ErrorCode.INTERNAL_ERROR,
            message="Something went wrong on our side. The issue has been logged.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def _summarise_validation(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Flatten pydantic errors into a compact, client friendly list.

    Field paths can contain tuple indexes which are not JSON serialisable.
    """
    summary: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        summary.append(
            {
                "field": ".".join(loc) or "request",
                "message": error.get("msg", "is invalid"),
                "type": error.get("type", "value_error"),
            }
        )
    return summary
