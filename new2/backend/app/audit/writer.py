"""Audit writer (PRD 28).

One function, one call shape. Services call :func:`record` inside the same
transaction as the change they are describing, so an audit row can never be
committed without its change and vice versa.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.logging_config import correlation_id_var, redact_value, trace_id_var
from app.models.audit import AuditAction, AuditLog, EntityType

logger = logging.getLogger(__name__)


def record(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: UUID | None,
    actor_name: str,
    action: AuditAction,
    entity_type: EntityType,
    entity_id: str | UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    trace_id: str | None = None,
    is_system_action: bool = False,
) -> AuditLog:
    """Append one audit row.

    ``before``/``after`` are scrubbed through the log redactor so raw customer
    values can never be persisted into the audit trail (PRD 30).
    """
    entity_id_str = str(entity_id) if entity_id is not None else None
    changed = _changed_fields(before, after)

    entry = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_name=actor_name or "system",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id_str,
        before_json=redact_value(before) if before else None,
        after_json=redact_value(after) if after else None,
        changed_fields=",".join(changed) if changed else None,
        reason=reason,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
        trace_id=trace_id or trace_id_var.get(),
        correlation_id=correlation_id_var.get(),
        is_system_action=is_system_action,
    )
    session.add(entry)
    return entry


def record_system(
    session: Session,
    *,
    tenant_id: str,
    action: AuditAction,
    entity_type: EntityType,
    entity_id: str | UUID | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AuditLog:
    """Audit row for an automated action (sync job, scheduler, worker)."""
    return record(
        session,
        tenant_id=tenant_id,
        actor_user_id=None,
        actor_name="system",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        before=before,
        after=after,
        is_system_action=True,
        **kwargs,
    )


def _changed_fields(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    if before is None and after is None:
        return []
    before = before or {}
    after = after or {}
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))
