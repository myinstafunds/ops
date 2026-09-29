"""Work item, event, comment, communication, audit and stats schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.communication import (
    CallDisposition,
    Channel,
    CommunicationStatus,
    Direction,
)
from app.models.org import SourceApp
from app.models.sla import SlaStatus
from app.models.work import Priority, WorkStatus

ReasonField = Annotated[str, Field(min_length=3, max_length=1000)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# work items
# --------------------------------------------------------------------------
class LockInfo(BaseModel):
    is_locked: bool
    is_locked_by_me: bool = False
    locked_by_user_id: uuid.UUID | None = None
    locked_by_name: str | None = None
    locked_at: datetime | None = None
    locked_until: datetime | None = None
    seconds_remaining: int | None = None
    is_expired: bool = False
    last_heartbeat_at: datetime | None = None


class WorkItemSummary(BaseModel):
    id: uuid.UUID
    source_app: SourceApp
    source_key: str | None = None
    external_ref: str | None = None
    deep_link: str | None = None
    title: str
    customer_name: str | None = None
    customer_mobile_masked: str | None = None
    queue_code: str
    department_code: str | None = None
    status: WorkStatus
    priority: Priority
    sla_status: SlaStatus
    assigned_user_id: uuid.UUID | None = None
    assigned_user_name: str | None = None
    due_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    last_activity_at: datetime | None = None
    lock: LockInfo
    age_minutes: int | None = None
    is_overdue: bool = False


class WorkItemDetail(WorkItemSummary):
    assigned_at: datetime | None = None
    assignment_is_manual: bool = False
    allocation_explanation: str | None = None
    sla_breached_at: datetime | None = None
    due_at_overridden: bool = False
    sla_policy_id: uuid.UUID | None = None
    closure_outcome: str | None = None
    closure_notes: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    open_comment_count: int = 0
    last_synced_at: datetime | None = None
    source_fields: dict[str, Any] = Field(default_factory=dict)


class WorkItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Annotated[str, Field(min_length=3, max_length=300)]
    queue_code: Annotated[str, Field(min_length=2, max_length=64)]
    source_app: SourceApp = SourceApp.OPS
    source_key: Annotated[str | None, Field(max_length=255)] = None
    external_ref: Annotated[str | None, Field(max_length=128)] = None
    deep_link: Annotated[str | None, Field(max_length=500)] = None
    customer_name: Annotated[str | None, Field(max_length=200)] = None
    customer_mobile: Annotated[str | None, Field(max_length=32)] = None
    priority: Priority = Priority.MEDIUM
    due_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    auto_assign: bool = False


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: uuid.UUID
    reason: ReasonField


class UnassignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: ReasonField


class TransferRequest(BaseModel):
    """Transfer destination is required and the reason is mandatory (PRD 20)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    queue_code: Annotated[str | None, Field(max_length=64)] = None
    user_id: uuid.UUID | None = None
    reason: ReasonField

    @model_validator(mode="after")
    def _require_destination(self) -> TransferRequest:
        if not self.queue_code and not self.user_id:
            raise ValueError("Provide a destination queue, a destination user, or both.")
        return self


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: ReasonField
    outcome: Annotated[str | None, Field(max_length=120)] = None
    notes: Annotated[str | None, Field(max_length=2000)] = None


class StatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: WorkStatus
    reason: ReasonField


class ReleaseLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: ReasonField


class HeartbeatResponse(BaseModel):
    work_item_id: uuid.UUID
    locked_until: datetime
    seconds_remaining: int
    lock_count: int
    last_heartbeat_at: datetime


class DueAtOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    due_at: datetime
    reason: ReasonField


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
class WorkItemEventItem(BaseModel):
    id: uuid.UUID
    event_type: str
    actor_user_id: uuid.UUID | None = None
    actor_name: str
    from_value: str | None = None
    to_value: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, alias="metadata_json")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @field_validator("metadata", mode="before")
    @classmethod
    def _default_metadata(cls, value: object) -> object:
        """A NULL ``metadata_json`` column must still serialise as an object.

        Events written outside the enriched paths (a status change, a lock) leave
        the column NULL. Declaring the field ``dict | None`` would leak that to
        clients as ``null``; coercing here keeps the wire contract "always an
        object", which is what the frontend timeline can rely on.
        """
        return {} if value is None else value


# --------------------------------------------------------------------------
# comments
# --------------------------------------------------------------------------
class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comment: Annotated[str, Field(min_length=1, max_length=5000)]


class CommentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comment: Annotated[str, Field(min_length=1, max_length=5000)]
    edit_reason: ReasonField


class CommentDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: ReasonField


class CommentItem(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    author_user_id: uuid.UUID | None = None
    author_name: str
    comment: str
    created_at: datetime
    updated_at: datetime
    edit_count: int
    is_deleted: bool
    deleted_at: datetime | None = None
    can_edit: bool = False
    can_delete: bool = False


# --------------------------------------------------------------------------
# eligibility / allocation
# --------------------------------------------------------------------------
class CandidateItem(BaseModel):
    user_id: uuid.UUID
    display_name: str
    username: str
    current_workload: int
    capacity: int
    priority_weight: int
    specialisation_match: bool
    is_available: bool
    daily_allocations_today: int
    eligible: bool
    ineligible_reasons: list[str] = Field(default_factory=list)


class EligibilityResponse(BaseModel):
    queue_code: str
    candidates: list[CandidateItem]
    eligible_count: int
    checked_at: datetime


class AutoAssignResponse(BaseModel):
    work_item: WorkItemDetail
    assignee_user_id: uuid.UUID
    assignee_name: str
    explanation: str
    explanation_factors: list[str] = Field(default_factory=list)
    candidates_considered: int = 0


class ManualAssignResponse(BaseModel):
    work_item: WorkItemDetail
    explanation: str


# --------------------------------------------------------------------------
# communications
# --------------------------------------------------------------------------
class CommunicationItem(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID | None = None
    work_item_ref: str | None = None
    channel: Channel
    direction: Direction
    status: CommunicationStatus
    customer_reference: str | None = None
    customer_name: str | None = None
    #: Masked only. The raw value is never serialised (PRD 26).
    customer_mobile_masked: str | None = None
    actor_name: str | None = None
    subject: str | None = None
    body_preview: str | None = None
    outcome_notes: str | None = None
    disposition: CallDisposition | None = None
    provider_message_id: str | None = None
    retry_count: int
    occurred_at: datetime
    delivered_at: datetime | None = None
    duration_seconds: int | None = None
    created_at: datetime


class CommunicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel: Channel
    direction: Direction
    work_item_id: uuid.UUID | None = None
    customer_reference: Annotated[str | None, Field(max_length=128)] = None
    customer_name: Annotated[str | None, Field(max_length=200)] = None
    customer_mobile: Annotated[str | None, Field(max_length=32)] = None
    subject: Annotated[str | None, Field(max_length=300)] = None
    body: Annotated[str | None, Field(max_length=5000)] = None
    outcome_notes: Annotated[str | None, Field(max_length=2000)] = None
    disposition: CallDisposition | None = None
    status: CommunicationStatus = CommunicationStatus.PENDING
    occurred_at: datetime | None = None
    duration_seconds: Annotated[int | None, Field(ge=0, le=86400)] = None

    @model_validator(mode="after")
    def _channel_requirements(self) -> CommunicationCreate:
        if self.channel is Channel.CALL and self.disposition is None:
            raise ValueError("disposition is required for CALL communications")
        if (
            self.channel is Channel.CALL
            and self.direction is Direction.INBOUND
            and not self.duration_seconds
        ):
            raise ValueError("duration_seconds is required for inbound calls")
        return self


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
class AuditLogItem(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    actor_name: str
    action: str
    entity_type: str
    entity_id: str | None = None
    before: dict[str, Any] | None = Field(default=None, alias="before_json")
    after: dict[str, Any] | None = Field(default=None, alias="after_json")
    changed_fields: str | None = None
    reason: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    trace_id: str | None = None
    is_system_action: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
class SummaryStats(BaseModel):
    open_work: int = 0
    assigned_work: int = 0
    in_progress_work: int = 0
    unassigned_work: int = 0
    locked_cases: int = 0
    sla_breaches: int = 0
    due_today: int = 0
    unavailable_staff: int = 0
    active_queue_members: int = 0
    pending_approvals: int = 0
    open_queues: int = 0
    generated_at: datetime


class StatusCount(BaseModel):
    status: str
    count: int


class QueueWorkloadRow(BaseModel):
    queue_code: str
    queue_name: str
    source_app: SourceApp
    open_count: int
    unassigned_count: int
    assigned_count: int
    overdue_count: int
    eligible_members: int
    available_members: int
    capacity_utilisation_pct: float


class UserWorkloadRow(BaseModel):
    user_id: uuid.UUID
    display_name: str
    username: str
    is_available: bool
    open_workload: int
    capacity: int
    utilisation_pct: float
    locked_cases: int
    breached_cases: int
    department_codes: list[str] = Field(default_factory=list)


class AgeingBucket(BaseModel):
    label: str
    min_hours: int
    max_hours: int | None
    count: int


class AgeingResponse(BaseModel):
    buckets: list[AgeingBucket]
    total: int
    generated_at: datetime


class SlaStats(BaseModel):
    not_started: int = 0
    on_track: int = 0
    due_soon: int = 0
    breached: int = 0
    completed: int = 0
    total_open: int = 0
    breach_rate_pct: float = 0.0
    generated_at: datetime


class ActionAlert(BaseModel):
    key: str
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    title: str
    detail: str
    count: int
    link_path: str
    filter_params: dict[str, Any] = Field(default_factory=dict)


class DashboardResponse(BaseModel):
    summary: SummaryStats
    work_by_status: list[StatusCount]
    work_by_queue: list[QueueWorkloadRow]
    work_by_department: list[StatusCount]
    user_workload: list[UserWorkloadRow]
    ageing: AgeingResponse
    sla: SlaStats
    alerts: list[ActionAlert]
    integration_health: list[IntegrationHealthItem] = Field(default_factory=list)
    generated_at: datetime


class IntegrationHealthItem(BaseModel):
    source_app: SourceApp
    status: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_sync_at: datetime | None = None
    latency_ms: float | None = None
    error_count: int
    records_processed: int
    records_failed: int
    consecutive_failures: int
    last_error_summary: str | None = None


class SyncRunItem(BaseModel):
    id: uuid.UUID
    source_app: SourceApp
    status: str
    job_name: str
    queue_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    records_fetched: int
    records_created: int
    records_updated: int
    records_skipped: int
    records_failed: int
    error_summary: str | None = None
    trigger_type: str
    duration_seconds: float | None = None


DashboardResponse.model_rebuild()


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
class SearchResultItem(BaseModel):
    work_item_id: uuid.UUID
    source_app: SourceApp
    external_ref: str | None = None
    title: str
    customer_name: str | None = None
    customer_mobile_masked: str | None = None
    queue_code: str
    status: WorkStatus
    assigned_user_name: str | None = None
    deep_link: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
    total: int
