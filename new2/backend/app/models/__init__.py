"""SQLAlchemy model registry.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate and ``create_all`` rely on.
"""

from app.db import Base
from app.models.audit import AuditAction, AuditLog, EntityType
from app.models.availability import (
    AvailabilityRequestStatus,
    AvailabilityType,
    UserAvailability,
)
from app.models.base import metadata, new_uuid, utcnow
from app.models.communication import (
    CallDisposition,
    Channel,
    CommunicationEvent,
    CommunicationStatus,
    Direction,
)
from app.models.integration import (
    CaseSyncRun,
    IntegrationHealth,
    IntegrationHealthStatus,
    Notification,
    NotificationType,
    SyncRunStatus,
)
from app.models.org import (
    Department,
    DepartmentMembership,
    OrgStatus,
    Queue,
    QueueMembership,
    SourceApp,
)
from app.models.sla import (
    QueueSlaPolicy,
    SlaBreachAction,
    SlaPolicy,
    SlaStatus,
)
from app.models.user import AvailabilityStatus, OperationalStatus, User, UserRole
from app.models.work import (
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    CaseComment,
    EventType,
    WorkItem,
    WorkItemEvent,
    WorkStatus,
)

__all__ = [
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "AuditAction",
    "AuditLog",
    "AvailabilityRequestStatus",
    "AvailabilityStatus",
    "AvailabilityType",
    "Base",
    "CallDisposition",
    "CaseComment",
    "CaseSyncRun",
    "Channel",
    "CommunicationEvent",
    "CommunicationStatus",
    "Department",
    "DepartmentMembership",
    "Direction",
    "EntityType",
    "EventType",
    "IntegrationHealth",
    "IntegrationHealthStatus",
    "Notification",
    "NotificationType",
    "OperationalStatus",
    "OrgStatus",
    "Queue",
    "QueueMembership",
    "QueueSlaPolicy",
    "SlaBreachAction",
    "SlaPolicy",
    "SlaStatus",
    "SourceApp",
    "SyncRunStatus",
    "User",
    "UserAvailability",
    "UserRole",
    "WorkItem",
    "WorkItemEvent",
    "WorkStatus",
    "metadata",
    "new_uuid",
    "utcnow",
]
