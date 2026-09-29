"""Communication log service (PRD 26).

The stored mobile number is never selected into a response. Only
``mobile_masked`` leaves this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.audit import record
from app.errors import NotFoundError
from app.models.audit import AuditAction, EntityType
from app.models.communication import (
    Channel,
    CommunicationEvent,
    CommunicationStatus,
    Direction,
)
from app.models.work import WorkItem
from app.security.masking import mask_mobile
from app.services.org import WriteContext


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_communication(
    session: Session,
    *,
    tenant_id: str,
    data: dict[str, Any],
    ctx: WriteContext,
) -> CommunicationEvent:
    work_item: WorkItem | None = None
    work_item_id = data.get("work_item_id")
    if work_item_id is not None:
        work_item = session.scalar(
            select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.tenant_id == tenant_id)
        )
        if work_item is None:
            raise NotFoundError("Work item was not found.")

    mobile = data.get("customer_mobile")
    event = CommunicationEvent(
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        source_app=work_item.source_app.value if work_item else "OPS",
        channel=data["channel"],
        direction=data["direction"],
        status=data.get("status", CommunicationStatus.PENDING),
        customer_reference=data.get("customer_reference")
        or (work_item.external_ref if work_item else None),
        customer_name=data.get("customer_name") or (work_item.customer_name if work_item else None),
        customer_mobile=mobile,
        mobile_masked=mask_mobile(mobile),
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        subject=data.get("subject"),
        body_preview=_preview(data.get("body")),
        outcome_notes=data.get("outcome_notes"),
        disposition=data.get("disposition"),
        occurred_at=data.get("occurred_at") or utcnow(),
        duration_seconds=data.get("duration_seconds"),
    )
    session.add(event)
    session.flush()

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.COMMUNICATION_CREATED,
        entity_type=EntityType.COMMUNICATION,
        entity_id=event.id,
        after={
            "channel": event.channel.value,
            "direction": event.direction.value,
            "status": event.status.value,
            "work_item_id": str(work_item_id) if work_item_id else None,
            # Masked, because this lands in the audit trail.
            "customer_mobile": event.mobile_masked,
        },
        reason="Communication logged",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    return event


def list_communications(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID | None = None,
    channel: Channel | None = None,
    direction: Direction | None = None,
    status: CommunicationStatus | None = None,
    actor_user_id: uuid.UUID | None = None,
    customer_reference: str | None = None,
    search: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str = "occurred_at",
    descending: bool = True,
) -> tuple[list[CommunicationEvent], int]:
    query: Select[tuple[CommunicationEvent]] = select(CommunicationEvent).where(
        CommunicationEvent.tenant_id == tenant_id
    )
    if work_item_id is not None:
        query = query.where(CommunicationEvent.work_item_id == work_item_id)
    if channel is not None:
        query = query.where(CommunicationEvent.channel == channel)
    if direction is not None:
        query = query.where(CommunicationEvent.direction == direction)
    if status is not None:
        query = query.where(CommunicationEvent.status == status)
    if actor_user_id is not None:
        query = query.where(CommunicationEvent.actor_user_id == actor_user_id)
    if customer_reference:
        query = query.where(
            func.lower(func.coalesce(CommunicationEvent.customer_reference, ""))
            == customer_reference.strip().lower()
        )
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(func.coalesce(CommunicationEvent.customer_name, "")).like(pattern)
            | func.lower(func.coalesce(CommunicationEvent.customer_reference, "")).like(pattern)
            | func.lower(func.coalesce(CommunicationEvent.body_preview, "")).like(pattern)
            | func.lower(func.coalesce(CommunicationEvent.subject, "")).like(pattern)
        )
    if occurred_from is not None:
        query = query.where(CommunicationEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        query = query.where(CommunicationEvent.occurred_at <= occurred_to)

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = {
        "occurred_at": CommunicationEvent.occurred_at,
        "created_at": CommunicationEvent.created_at,
        "channel": CommunicationEvent.channel,
        "status": CommunicationEvent.status,
    }.get(sort, CommunicationEvent.occurred_at)
    order = column.desc() if descending else column.asc()
    rows = list(session.scalars(query.order_by(order).offset(offset).limit(limit)))
    return rows, total


def get_communication(
    session: Session, *, tenant_id: str, communication_id: uuid.UUID
) -> CommunicationEvent:
    event = session.scalar(
        select(CommunicationEvent).where(
            CommunicationEvent.id == communication_id,
            CommunicationEvent.tenant_id == tenant_id,
        )
    )
    if event is None:
        raise NotFoundError("That communication was not found.")
    return event


def update_status(
    session: Session,
    *,
    tenant_id: str,
    communication_id: uuid.UUID,
    status: CommunicationStatus,
    ctx: WriteContext,
    provider_message_id: str | None = None,
    error_code: str | None = None,
) -> CommunicationEvent:
    """Used by the delivery-receipt worker (Release 5)."""
    event = get_communication(session, tenant_id=tenant_id, communication_id=communication_id)
    before = {"status": event.status.value}
    event.status = status
    if status in (CommunicationStatus.DELIVERED, CommunicationStatus.CONNECTED):
        event.delivered_at = utcnow()
    if provider_message_id:
        event.provider_message_id = provider_message_id
    if error_code:
        event.error_code = error_code

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.COMMUNICATION_STATUS_UPDATED,
        entity_type=EntityType.COMMUNICATION,
        entity_id=event.id,
        before=before,
        after={"status": status.value},
        reason="Delivery status updated",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return event


def _preview(body: str | None, limit: int = 160) -> str | None:
    if not body:
        return None
    collapsed = " ".join(body.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}..."


__all__ = [
    "create_communication",
    "get_communication",
    "list_communications",
    "update_status",
]
