"""Integration health and sync run routes (PRD 25, 38).

Health is read from stored probe results. When an upstream is unreachable this
endpoint still answers - it reports the failure rather than propagating it, so
an LOS/LMS outage cannot take Ops down with it (PRD 4.1, Rule 9).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.dependencies.auth import (
    CurrentUserCtx,
    DbSession,
    RequestContext,
    require_permissions,
)
from app.errors import NotFoundError
from app.models.integration import CaseSyncRun, IntegrationHealth
from app.models.org import SourceApp
from app.schemas.common import ErrorResponse, PageResponse, page_meta
from app.schemas.work import IntegrationHealthItem, SyncRunItem
from app.security.permissions import Permission
from app.services import stats as stats_service

router = APIRouter(prefix="/integrations", tags=["integrations"])
sync_runs_router = APIRouter(prefix="/sync-runs", tags=["integrations"])

CanRead = require_permissions(Permission.OPS_INTEGRATION_READ)


def _health_item(row: IntegrationHealth) -> IntegrationHealthItem:
    return IntegrationHealthItem(
        source_app=row.source_app,
        status=row.status.value,
        last_success_at=row.last_success_at,
        last_failure_at=row.last_failure_at,
        last_sync_at=row.last_sync_at,
        latency_ms=row.latency_ms,
        error_count=row.error_count,
        records_processed=row.records_processed,
        records_failed=row.records_failed,
        consecutive_failures=row.consecutive_failures,
        last_error_summary=row.last_error_summary,
    )


def _sync_run_item(row: CaseSyncRun) -> SyncRunItem:
    duration: float | None = None
    if row.started_at and row.completed_at:
        duration = round((row.completed_at - row.started_at).total_seconds(), 2)
    return SyncRunItem(
        id=row.id,
        source_app=row.source_app,
        status=row.status.value,
        job_name=row.job_name,
        queue_code=row.queue_code,
        started_at=row.started_at,
        completed_at=row.completed_at,
        records_fetched=row.records_fetched,
        records_created=row.records_created,
        records_updated=row.records_updated,
        records_skipped=row.records_skipped,
        records_failed=row.records_failed,
        error_summary=row.error_summary,
        trigger_type=row.trigger_type,
        duration_seconds=duration,
    )


@router.get(
    "/health",
    response_model=list[IntegrationHealthItem],
    responses={403: {"model": ErrorResponse}},
    summary="Health of every upstream integration",
)
def health(
    context: CurrentUserCtx, session: DbSession, _: Annotated[RequestContext, Depends(CanRead)]
) -> list[IntegrationHealthItem]:
    return stats_service.integration_health(session, tenant_id=context.tenant_id)


@router.get(
    "/{source_app}/health",
    response_model=IntegrationHealthItem,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Health of one upstream integration",
)
def source_health(
    source_app: SourceApp,
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
) -> IntegrationHealthItem:
    row = session.scalar(
        select(IntegrationHealth).where(
            IntegrationHealth.tenant_id == context.tenant_id,
            IntegrationHealth.source_app == source_app,
        )
    )
    if row is None:
        raise NotFoundError(
            f"No health data has been recorded for {source_app.value} yet. "
            "The health probe has not run for this source."
        )
    return _health_item(row)


@sync_runs_router.get(
    "",
    response_model=PageResponse[SyncRunItem],
    responses={403: {"model": ErrorResponse}},
    summary="Sync run history",
)
def sync_runs(
    context: CurrentUserCtx,
    session: DbSession,
    _: Annotated[RequestContext, Depends(CanRead)],
    source_app: SourceApp | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> PageResponse[SyncRunItem]:
    query = select(CaseSyncRun).where(CaseSyncRun.tenant_id == context.tenant_id)
    if source_app is not None:
        query = query.where(CaseSyncRun.source_app == source_app)
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(CaseSyncRun.started_at.desc().nullslast())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return PageResponse(
        data=[_sync_run_item(row) for row in rows],
        meta=page_meta(page, page_size, total),
        trace_id=context.trace_id,
    )
