"""Structured logging and the request correlation middleware.

Every request receives a ``trace_id``, ``correlation_id`` and ``request_id``
(PRD 42). Log lines are JSON in non-development environments so they can be
shipped without a parsing step.

PII rules (PRD 42): the log filter drops anything that looks like a mobile
number, PAN, Aadhaar number or bank account so a careless call to
``logger.info(payload)`` cannot leak customer data.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)

TRACE_HEADER = "X-Trace-Id"
CORRELATION_HEADER = "X-Correlation-Id"
REQUEST_HEADER = "X-Request-Id"

# Patterns that must never reach a log sink (PRD 30 / 42).
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mobile", re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")),
    ("aadhaar", re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")),
    ("pan", re.compile(r"(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])")),
    ("bank_account", re.compile(r"(?<!\d)\d{12,18}(?!\d)")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")),
)
REDACTION = "***redacted***"

_SENSITIVE_LOG_KEYS = frozenset(
    {
        "password",
        "passcode",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "mfa_code",
        "otp",
        "customer_mobile",
        "mobile",
        "mobile_number",
        "phone",
        "pan",
        "pan_number",
        "aadhaar",
        "aadhaar_number",
        "bank_account",
        "account_number",
        "payload_json",
        "customer_payload",
    }
)


def redact_text(value: str) -> str:
    """Replace PII shaped substrings in free text."""
    redacted = value
    for _, pattern in _PII_PATTERNS:
        redacted = pattern.sub(REDACTION, redacted)
    return redacted


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact PII from a structure about to be logged."""
    if _depth > 8:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_LOG_KEYS:
                result[str(key)] = REDACTION
            else:
                result[str(key)] = redact_value(item, _depth=_depth + 1)
        return result
    if isinstance(value, list | tuple | set):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class PiiFilter(logging.Filter):
    """Last line of defence applied to every emitted record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_value(dict(record.args))
            else:
                record.args = tuple(redact_value(arg) for arg in record.args)
        return True


_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace_id_var.get(),
            "correlation_id": correlation_id_var.get(),
            "request_id": request_id_var.get(),
            "actor_user_id": actor_id_var.get(),
            "tenant_id": tenant_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"{record.name} [{trace_id_var.get() or '-'}] {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.ops_log_format == "json" else ConsoleFormatter()
    )
    handler.addFilter(PiiFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.ops_log_level)

    # Uvicorn's own access log duplicates our middleware output.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("uvicorn.error").handlers.clear()
    logging.getLogger("uvicorn.error").propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.ops_log_sql else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign and propagate correlation identifiers, then log the outcome."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        request_id = uuid.uuid4().hex

        trace_token = trace_id_var.set(trace_id)
        corr_token = correlation_id_var.set(correlation_id)
        req_token = request_id_var.set(request_id)
        request.state.trace_id = trace_id
        request.state.correlation_id = correlation_id
        request.state.request_id = request_id
        request.state.started_at = time.perf_counter()

        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(trace_token)
            correlation_id_var.reset(corr_token)
            request_id_var.reset(req_token)

        duration_ms = (time.perf_counter() - request.state.started_at) * 1000
        response.headers[TRACE_HEADER] = trace_id
        response.headers[CORRELATION_HEADER] = correlation_id
        response.headers[REQUEST_HEADER] = request_id

        actor = getattr(request.state, "actor_user_id", None)
        logging.getLogger("app.request").info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "actor_user_id": actor,
                "query": redact_text(request.url.query or ""),
            },
        )
        return response


def new_trace_id() -> str:
    return uuid.uuid4().hex
