"""Dashboard and statistics routes (PRD 9, 38)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.schemas.common import ErrorResponse
from app.schemas.work import (
    AgeingResponse,
    DashboardResponse,
    QueueWorkloadRow,
    SlaStats,
    SummaryStats,
    UserWorkloadRow,
)
from app.security.permissions import Permission
from app.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["dashboard"])

CanReadDashboard = require_permissions(Permission.OPS_DASHBOARD_READ)
CanReadCases = require_permissions(Permission.OPS_CASES_READ)


@router.get(
    "/summary",
    response_model=SummaryStats,
    responses={403: {"model": ErrorResponse}},
    summary="Headline operational counters",
)
def summary(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
) -> SummaryStats:
    return stats_service.summary(session, tenant_id=context.tenant_id)


@router.get(
    "/workload",
    response_model=list[QueueWorkloadRow],
    responses={403: {"model": ErrorResponse}},
    summary="Per-queue workload and capacity utilisation",
)
def workload(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
) -> list[QueueWorkloadRow]:
    return [
        QueueWorkloadRow(**row)
        for row in stats_service.queue_workload(session, tenant_id=context.tenant_id)
    ]


@router.get(
    "/workload/users",
    response_model=list[UserWorkloadRow],
    responses={403: {"model": ErrorResponse}},
    summary="Per-user workload and utilisation",
)
def user_workload(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
    limit: int = 20,
) -> list[UserWorkloadRow]:
    return stats_service.user_workload(
        session, tenant_id=context.tenant_id, limit=min(max(limit, 1), 100)
    )


@router.get(
    "/ageing",
    response_model=AgeingResponse,
    responses={403: {"model": ErrorResponse}},
    summary="Ageing buckets for open work",
)
def ageing(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
) -> AgeingResponse:
    return stats_service.ageing(session, tenant_id=context.tenant_id)


@router.get(
    "/sla",
    response_model=SlaStats,
    responses={403: {"model": ErrorResponse}},
    summary="SLA distribution and breach rate",
)
def sla(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
) -> SlaStats:
    return stats_service.sla_stats(session, tenant_id=context.tenant_id)


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    responses={403: {"model": ErrorResponse}},
    summary="Everything the dashboard needs in one round trip",
)
def dashboard(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanReadDashboard)],
) -> DashboardResponse:
    """One call keeps the dashboard under the 1.5s target (PRD 50)."""
    tenant_id = context.tenant_id
    summary_stats = stats_service.summary(session, tenant_id=tenant_id)
    queue_rows = stats_service.queue_workload(session, tenant_id=tenant_id)
    workload_rows = stats_service.user_workload(session, tenant_id=tenant_id, limit=20)
    ageing_stats = stats_service.ageing(session, tenant_id=tenant_id)
    sla = stats_service.sla_stats(session, tenant_id=tenant_id)
    health = stats_service.integration_health(session, tenant_id=tenant_id)
    alerts = stats_service.build_alerts(
        summary_stats=summary_stats,
        queue_rows=queue_rows,
        workload_rows=workload_rows,
        health=health,
        lock_expiring_soon=stats_service.locks_expiring_within(
            session, tenant_id=tenant_id, minutes=10
        ),
    )
    return DashboardResponse(
        summary=summary_stats,
        work_by_status=stats_service.work_by_status(session, tenant_id=tenant_id),
        work_by_queue=[QueueWorkloadRow(**row) for row in queue_rows],
        work_by_department=stats_service.work_by_department(session, tenant_id=tenant_id),
        user_workload=workload_rows,
        ageing=ageing_stats,
        sla=sla,
        alerts=alerts,
        integration_health=health,
        generated_at=summary_stats.generated_at,
    )
