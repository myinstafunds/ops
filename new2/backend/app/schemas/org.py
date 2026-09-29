"""Auth, user, department, queue and availability schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.availability import AvailabilityRequestStatus, AvailabilityType
from app.models.org import OrgStatus, SourceApp
from app.models.user import OperationalStatus

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=200)]
NameField = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9 _\-()/.]+$")]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: Annotated[str, Field(min_length=1, max_length=150)]
    password: Annotated[str, Field(min_length=1, max_length=200)]
    tenant_id: str | None = Field(default=None, max_length=64)


class LoginResponse(BaseModel):
    """Either ``access_token`` or ``mfa_required`` is present."""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    mfa_required: bool = False
    mfa_token: str | None = None
    user: CurrentUser | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: Annotated[str, Field(min_length=10)]
    code: Annotated[str, Field(min_length=4, max_length=12)]


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class CurrentUserPermissions(BaseModel):
    roles: list[str]
    permissions: list[str]


class CurrentUser(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None = None
    tenant_id: str
    status: OperationalStatus
    is_available: bool
    capacity: int
    bre_user_id: str | None = None
    bre_roles: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    queues: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
class UserSummary(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None = None
    status: OperationalStatus
    is_available: bool
    capacity: int
    priority_weight: int
    tenant_id: str
    job_title: str | None = None
    roles: list[str] = Field(default_factory=list)
    bre_roles: list[str] = Field(default_factory=list)


class UserListItem(UserSummary):
    open_workload: int = 0
    department_codes: list[str] = Field(default_factory=list)
    queue_codes: list[str] = Field(default_factory=list)
    current_availability: str | None = None


class UserDetail(UserListItem):
    phone_masked: str | None = None
    specialisations: list[str] = Field(default_factory=list)
    daily_allocation_limit: int = 0
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    permissions: list[str] = Field(default_factory=list)
    locked_case_count: int = 0
    available_now: bool = False
    unavailable_until: datetime | None = None


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: Annotated[str, Field(min_length=3, max_length=150)]
    display_name: NameField
    email: EmailStr | None = None
    bre_user_id: Annotated[str | None, Field(max_length=128)] = None
    job_title: Annotated[str | None, Field(max_length=150)] = None
    capacity: Annotated[int, Field(ge=0, le=500)] = 25
    priority_weight: Annotated[int, Field(ge=0, le=100)] = 0
    daily_allocation_limit: Annotated[int, Field(ge=0, le=1000)] = 50
    specialisations: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list)
    roles: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list)
    is_available: bool = True


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: NameField | None = None
    email: EmailStr | None = None
    job_title: Annotated[str | None, Field(max_length=150)] = None
    capacity: Annotated[int | None, Field(ge=0, le=500)] = None
    priority_weight: Annotated[int | None, Field(ge=0, le=100)] = None
    daily_allocation_limit: Annotated[int | None, Field(ge=0, le=1000)] = None
    specialisations: list[Annotated[str, Field(max_length=120)]] | None = None
    is_available: bool | None = None
    status: OperationalStatus | None = None


class UserStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=3, max_length=1000)]


class UserRoleAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[Annotated[str, Field(max_length=64)]] = Field(min_length=1, max_length=10)


# --------------------------------------------------------------------------
# departments
# --------------------------------------------------------------------------
class DepartmentSummary(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    source_app: SourceApp
    status: OrgStatus
    member_count: int = 0


class DepartmentDetail(DepartmentSummary):
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Annotated[str, Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9_]+$")]
    name: NameField
    source_app: SourceApp = SourceApp.OPS
    description: Annotated[str | None, Field(max_length=1000)] = None


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: NameField | None = None
    status: OrgStatus | None = None
    description: Annotated[str | None, Field(max_length=1000)] = None


class DepartmentMembershipItem(ORMModel):
    id: uuid.UUID
    department_id: uuid.UUID
    department_code: str
    department_name: str
    user_id: uuid.UUID
    user_name: str
    username: str
    is_primary: bool
    is_active: bool
    created_at: datetime


class DepartmentMembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: uuid.UUID
    user_id: uuid.UUID
    is_primary: bool = False


class DepartmentMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_primary: bool | None = None
    is_active: bool | None = None


# --------------------------------------------------------------------------
# queues
# --------------------------------------------------------------------------
class QueueSummary(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    source_app: SourceApp
    status: OrgStatus
    priority: int
    max_workload_per_user: int
    open_items: int = 0
    available_members: int = 0
    member_count: int = 0


class QueueDetail(QueueSummary):
    description: str | None = None
    department_code: str | None = None
    eligible_roles: list[str] = Field(default_factory=list)
    required_specialisations: list[str] = Field(default_factory=list)
    sla_policy_id: uuid.UUID | None = None
    sla_target_minutes: int | None = None
    sla_warning_minutes: int | None = None
    sync_interval_minutes: int | None = None
    created_at: datetime
    updated_at: datetime


class QueueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Z0-9_]+$")]
    name: NameField
    source_app: SourceApp = SourceApp.OPS
    department_id: uuid.UUID | None = None
    description: Annotated[str | None, Field(max_length=1000)] = None
    max_workload_per_user: Annotated[int, Field(ge=1, le=500)] = 25
    priority: Annotated[int, Field(ge=0, le=1000)] = 100
    eligible_roles: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list)
    required_specialisations: list[Annotated[str, Field(max_length=120)]] = Field(
        default_factory=list
    )
    sla_policy_id: uuid.UUID | None = None
    sync_interval_minutes: Annotated[int | None, Field(ge=1, le=10080)] = None


class QueueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: NameField | None = None
    status: OrgStatus | None = None
    department_id: uuid.UUID | None = None
    description: Annotated[str | None, Field(max_length=1000)] = None
    max_workload_per_user: Annotated[int | None, Field(ge=1, le=500)] = None
    priority: Annotated[int | None, Field(ge=0, le=1000)] = None
    eligible_roles: list[Annotated[str, Field(max_length=64)]] | None = None
    required_specialisations: list[Annotated[str, Field(max_length=120)]] | None = None
    sla_policy_id: uuid.UUID | None = None
    sync_interval_minutes: Annotated[int | None, Field(ge=1, le=10080)] = None


class QueueMembershipItem(ORMModel):
    id: uuid.UUID
    queue_id: uuid.UUID
    queue_code: str
    queue_name: str
    user_id: uuid.UUID
    user_name: str
    username: str
    is_active: bool
    capacity: int | None = None
    priority_weight: int
    daily_max_allocations: int | None = None
    specialisation: str | None = None
    shift_start: str | None = None
    shift_end: str | None = None
    open_workload: int = 0
    available_now: bool = False


class QueueMembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_id: uuid.UUID
    user_id: uuid.UUID
    capacity: Annotated[int | None, Field(ge=0, le=500)] = None
    priority_weight: Annotated[int, Field(ge=0, le=100)] = 0
    daily_max_allocations: Annotated[int | None, Field(ge=0, le=1000)] = None
    specialisation: Annotated[str | None, Field(max_length=120)] = None
    shift_start: str | None = None
    shift_end: str | None = None
    shift_days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=list)

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _time_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split(":")
        if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
            raise ValueError("must be HH:MM or HH:MM:SS")
        return value

    @field_validator("shift_days")
    @classmethod
    def _unique_days(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("shift_days must be unique")
        return sorted(value)


class QueueMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    capacity: Annotated[int | None, Field(ge=0, le=500)] = None
    priority_weight: Annotated[int | None, Field(ge=0, le=100)] = None
    daily_max_allocations: Annotated[int | None, Field(ge=0, le=1000)] = None
    specialisation: Annotated[str | None, Field(max_length=120)] = None
    shift_start: str | None = None
    shift_end: str | None = None
    shift_days: list[Annotated[int, Field(ge=0, le=6)]] | None = None


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------
class AvailabilityItem(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    username: str
    availability_type: AvailabilityType
    status: AvailabilityRequestStatus
    start_at: datetime
    end_at: datetime
    reason: str | None = None
    notes: str | None = None
    created_by_name: str | None = None
    created_at: datetime
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    is_active_window: bool = False
    blocks_allocation: bool = False


class AvailabilityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: uuid.UUID
    availability_type: AvailabilityType
    start_at: datetime
    end_at: datetime
    reason: Annotated[str, Field(min_length=3, max_length=1000)]
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("end_at")
    @classmethod
    def _end_after_start(cls, value: datetime, info) -> datetime:  # type: ignore[no-untyped-def]
        start = info.data.get("start_at")
        if start and value <= start:
            raise ValueError("end_at must be after start_at")
        return value


class AvailabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    availability_type: AvailabilityType | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    reason: Annotated[str | None, Field(min_length=3, max_length=1000)] = None
    notes: Annotated[str | None, Field(max_length=2000)] = None
    change_reason: Annotated[str, Field(min_length=3, max_length=1000)]


class AvailabilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str, Field(min_length=3, max_length=1000)]


class AvailabilityCreateForSelf(BaseModel):
    """Self-service request - created on behalf of the signed in user."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    availability_type: AvailabilityType
    start_at: datetime
    end_at: datetime
    reason: Annotated[str, Field(min_length=3, max_length=1000)]
    notes: Annotated[str | None, Field(max_length=2000)] = None


LoginResponse.model_rebuild()
