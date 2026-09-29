"""FastAPI application factory and ASGI entrypoint.

Responsibilities, in order:

1. Install the request-correlation middleware so every response carries a
   ``trace_id`` (PRD 42).
2. Enforce transport level protections: CORS allow-list, security headers and a
   hard request body ceiling (PRD 33).
3. Mount the routers under ``/api``.
4. Register the single error handler set from :mod:`app.errors`, so *every*
   failure - including ones raised inside FastAPI itself - uses the PRD 36
   envelope.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.db import SessionLocal
from app.errors import ErrorCode, register_exception_handlers
from app.logging_config import RequestContextMiddleware, configure_logging
from app.routers import ALL_ROUTERS
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)

#: Reused by the OpenAPI generator so every documented failure looks the same.
ERROR_RESPONSES: dict[int, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "Malformed request."},
    401: {"model": ErrorResponse, "description": "Missing, malformed or expired token."},
    403: {"model": ErrorResponse, "description": "Authenticated but not permitted."},
    404: {"model": ErrorResponse, "description": "Record not found in this tenant."},
    409: {
        "model": ErrorResponse,
        "description": "Conflict: case locked, no eligible user, invalid transition.",
    },
    413: {"model": ErrorResponse, "description": "Request body too large."},
    422: {"model": ErrorResponse, "description": "Validation failed."},
    429: {"model": ErrorResponse, "description": "Rate limited."},
    500: {"model": ErrorResponse, "description": "Unhandled server error."},
}

DESCRIPTION = """
Ops Control Center API.

**Authentication.** Every `/api` route except `/api/health` and `/api/auth/*`
requires a bearer token issued by the BRE Identity service. The token is the
only source of tenant scope: request bodies, query strings and headers cannot
widen it (PRD Rule 4).

**Authorisation.** The API is authoritative. A `403` means the signed token
lacks the permission the route declares; hiding a button in the UI is a
courtesy, never a control (PRD Rule 2).

**Errors.** Every failure body is `{"error": {...}, "trace_id": "..."}`.
"""


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defensive headers on every response (PRD 33).

    The SPA is served separately, so the policy is deliberately strict: no
    framing, no MIME sniffing, and no referrer leakage of signed URLs.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Cache-Control", "no-store")
        return response


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are buffered (PRD 33).

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the guard
    has to wrap ``receive`` and count bytes as they stream in. ``Content-Length``
    is checked first because it is free; the streaming counter is the backstop
    for chunked requests that omit it.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        trace_id = state.get("trace_id")

        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send, trace_id)
                    return
            except ValueError:
                await self._reject(send, trace_id)
                return

        received = 0

        async def _counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise PayloadTooLargeError
            return message

        try:
            await self.app(scope, _counting_receive, send)
        except PayloadTooLargeError:
            await self._reject(send, trace_id)

    async def _reject(self, send: Send, trace_id: str | None) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": ErrorCode.PAYLOAD_TOO_LARGE,
                    "message": "The request is larger than this API accepts.",
                    "details": {"max_bytes": self.max_bytes},
                },
                "trace_id": trace_id,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-content-type-options", b"nosniff"),
                    *([(b"x-trace-id", trace_id.encode())] if trace_id else []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class PayloadTooLargeError(Exception):
    """Internal signal raised by the streaming size guard."""


def _header(scope: Scope, name: bytes) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _database_ready() -> tuple[bool, float | None, str | None]:
    """Round trip the database with a statement timeout so readiness cannot hang."""
    started = time.perf_counter()
    try:
        with SessionLocal() as session:
            session.execute(text("SET LOCAL statement_timeout = 3000"))
            session.execute(text("SELECT 1"))
            session.commit()
        return True, (time.perf_counter() - started) * 1000, None
    except Exception as exc:  # pragma: no cover - only when the database is down
        logger.warning("readiness_check_failed error=%s", type(exc).__name__)
        return False, None, type(exc).__name__


def _health_body(request: Request | None) -> tuple[dict[str, object], int]:
    db_ok, latency_ms, error = _database_ready()
    checks: dict[str, Any] = {
        "database": {
            "status": "up" if db_ok else "down",
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            **({"error": error} if error else {}),
        }
    }
    if not db_ok:
        # BRE is the identity authority. If it is down nobody can sign in, but
        # already issued sessions keep working, so this is not a hard failure.
        checks["bre_identity"] = {"status": "unknown", "required_for": "new sign-ins"}
    body: MutableMapping[str, object] = {
        "status": "ready" if db_ok else "not_ready",
        "version": settings.app_version,
        "checks": checks,
    }
    body["trace_id"] = getattr(request.state, "trace_id", None) if request else None
    return dict(body), status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE


def create_health_router() -> APIRouter:
    """Unauthenticated probes.

    The orchestrator and the load balancer must be able to probe without a
    credential, and the payload contains no tenant data.
    """
    router = APIRouter(tags=["health"])

    @router.get("/health/live", summary="Liveness")
    async def live() -> dict[str, object]:
        """Process is up. Deliberately does not touch the database."""
        return {"status": "alive", "version": settings.app_version, "env": settings.ops_env}

    @router.get("/health/ready", summary="Readiness")
    async def ready(request: Request) -> JSONResponse:
        """Process is up *and* can serve traffic."""
        body, code = _health_body(request)
        return JSONResponse(status_code=code, content=body)

    @router.get("/health", summary="Health")
    async def healthz(request: Request) -> JSONResponse:
        """PRD 36 alias kept for the platform health check."""
        body, code = _health_body(request)
        return JSONResponse(status_code=code, content=body)

    return router


def apply_error_responses(app: FastAPI) -> None:
    """Stamp the shared error envelope onto every documented route."""
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        responses = dict(route.responses)
        for code, payload in ERROR_RESPONSES.items():
            # Never overwrite a route's own, more specific documentation.
            responses.setdefault(code, payload)
        route.responses = responses


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # Middleware is applied bottom-up. RequestContextMiddleware is added last so
    # that it wraps everything: a trace_id must exist even for CORS preflights,
    # 413 rejections and 404s.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.ops_max_body_bytes)
    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Trace-Id", "X-Correlation-Id"],
            expose_headers=["X-Trace-Id", "X-Correlation-Id", "X-Request-Id"],
            max_age=600,
        )

    register_exception_handlers(app)

    health = create_health_router()
    app.include_router(health)
    app.include_router(health, prefix=settings.api_prefix)

    for router in ALL_ROUTERS:
        app.include_router(router, prefix=settings.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, object]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "api": settings.api_prefix,
            "docs": None if settings.is_production else "/docs",
        }

    apply_error_responses(app)

    logger.info(
        "application_started env=%s auth_mode=%s local_auth=%s writeback_los=%s writeback_lms=%s",
        settings.ops_env,
        settings.ops_auth_mode,
        settings.ops_local_auth_enabled,
        settings.ops_feature_los_writeback,
        settings.ops_feature_lms_writeback,
    )
    return app


app = create_app()
