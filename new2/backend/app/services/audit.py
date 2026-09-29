"""Audit log query service. The audit table is read-only from here on."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models.audit import AuditAction, AuditLog, EntityType


def list_audit_entries(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    actions: list[AuditAction] | None = None,
    entity_type: EntityType | None = None,
    entity_id: str | None = None,
    work_item_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[AuditLog], int]:
    query: Select[tuple[AuditLog]] = select(AuditLog).where(AuditLog.tenant_id == tenant_id)

    if actor_user_id is not None:
        query = query.where(AuditLog.actor_user_id == actor_user_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if actions:
        query = query.where(AuditLog.action.in_(tuple(actions)))
    if entity_type is not None:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if work_item_id is not None:
        query = query.where(
            AuditLog.entity_type == EntityType.WORK_ITEM, AuditLog.entity_id == str(work_item_id)
        )
    if from_at is not None:
        query = query.where(AuditLog.created_at >= from_at)
    if to_at is not None:
        query = query.where(AuditLog.created_at <= to_at)
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(AuditLog.actor_name).like(pattern)
            | func.lower(AuditLog.action.value).like(pattern)
            | func.lower(func.coalesce(AuditLog.reason, "")).like(pattern)
            | func.lower(func.coalesce(AuditLog.entity_id, "")).like(pattern)
        )

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = {
        "created_at": AuditLog.created_at,
        "action": AuditLog.action,
        "entity_type": AuditLog.entity_type,
        "actor_name": AuditLog.actor_name,
    }.get(sort, AuditLog.created_at)
    order = column.desc() if descending else column.asc()
    rows = list(session.scalars(query.order_by(order).offset(offset).limit(limit)))
    return rows, total


def get_audit_entry(session: Session, *, tenant_id: str, audit_id: uuid.UUID) -> AuditLog:
    entry = session.scalar(
        select(AuditLog).where(AuditLog.id == audit_id, AuditLog.tenant_id == tenant_id)
    )
    if entry is None:
        raise NotFoundError("That audit entry was not found.")
    return entry


def audit_trail_for_entity(
    session: Session, *, tenant_id: str, entity_type: EntityType, entity_id: str, limit: int = 100
) -> list[AuditLog]:
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return list(rows)


def available_actions() -> list[AuditAction]:
    return sorted(AuditAction, key=lambda item: item.value)


def available_entity_types() -> list[EntityType]:
    return sorted(EntityType, key=lambda item: item.value)


__all__ = [
    "audit_trail_for_entity",
    "available_actions",
    "available_entity_types",
    "get_audit_entry",
    "list_audit_entries",
]
