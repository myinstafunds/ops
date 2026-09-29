"""Central permission registry (PRD 11, 51, Appendix C).

The backend is authoritative. A permission string only exists if it is declared
here, which means:

* there is exactly one place to review the whole access surface,
* a typo in a router turns into a startup/import failure rather than a silent
  allow, and
* permission drift is detectable by a test that walks the routers.

``OPS_LOS_WRITEBACK`` / ``OPS_LMS_WRITEBACK`` exist so the Release 7 surface is
declared, but every route using them is additionally gated by the feature flag
and the global kill switch (PRD 44).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Permission(StrEnum):
    """Every permission string understood by Ops."""

    # --- dashboard -------------------------------------------------------
    OPS_DASHBOARD_READ = "OPS_DASHBOARD_READ"

    # --- users -----------------------------------------------------------
    OPS_USERS_READ = "OPS_USERS_READ"
    OPS_USERS_WRITE = "OPS_USERS_WRITE"

    # --- roles -----------------------------------------------------------
    OPS_ROLES_READ = "OPS_ROLES_READ"
    OPS_ROLES_WRITE = "OPS_ROLES_WRITE"

    # --- departments -----------------------------------------------------
    OPS_DEPARTMENTS_READ = "OPS_DEPARTMENTS_READ"
    OPS_DEPARTMENTS_WRITE = "OPS_DEPARTMENTS_WRITE"

    # --- queues ----------------------------------------------------------
    OPS_QUEUES_READ = "OPS_QUEUES_READ"
    OPS_QUEUES_WRITE = "OPS_QUEUES_WRITE"

    # --- availability ----------------------------------------------------
    OPS_AVAILABILITY_READ = "OPS_AVAILABILITY_READ"
    OPS_AVAILABILITY_WRITE = "OPS_AVAILABILITY_WRITE"
    OPS_AVAILABILITY_APPROVE = "OPS_AVAILABILITY_APPROVE"

    # --- work items ------------------------------------------------------
    OPS_CASES_READ = "OPS_CASES_READ"
    OPS_CASES_CREATE = "OPS_CASES_CREATE"
    OPS_CASES_ASSIGN = "OPS_CASES_ASSIGN"
    OPS_CASES_LOCK = "OPS_CASES_LOCK"
    OPS_CASES_TRANSFER = "OPS_CASES_TRANSFER"
    OPS_CASES_CLOSE = "OPS_CASES_CLOSE"
    OPS_CASES_COMMENT = "OPS_CASES_COMMENT"

    # --- communications --------------------------------------------------
    OPS_COMMUNICATION_READ = "OPS_COMMUNICATION_READ"
    OPS_COMMUNICATION_WRITE = "OPS_COMMUNICATION_WRITE"

    # --- reports / exports ------------------------------------------------
    OPS_REPORTS_READ = "OPS_REPORTS_READ"
    OPS_EXPORT_REQUEST = "OPS_EXPORT_REQUEST"
    OPS_EXPORT_APPROVE = "OPS_EXPORT_APPROVE"

    # --- control ----------------------------------------------------------
    OPS_AUDIT_READ = "OPS_AUDIT_READ"
    OPS_INTEGRATION_READ = "OPS_INTEGRATION_READ"
    OPS_SETTINGS_WRITE = "OPS_SETTINGS_WRITE"

    # --- controlled write-back (Release 7, disabled by default) -----------
    OPS_LOS_WRITEBACK = "OPS_LOS_WRITEBACK"
    OPS_LMS_WRITEBACK = "OPS_LMS_WRITEBACK"


ALL_PERMISSIONS: Final[frozenset[str]] = frozenset(p.value for p in Permission)

#: Permissions that unlock the navigation shell, in display order.
DASHBOARD_PERMISSIONS: Final[tuple[str, ...]] = (Permission.OPS_DASHBOARD_READ,)


class RoleKey(StrEnum):
    """Ops operational roles.

    These are *not* identity roles. BRE owns identity and its roles; these
    describe how an operator behaves inside Ops (PRD Rule 1).
    """

    OPS_ADMIN = "OPS_ADMIN"
    OPS_MANAGER = "OPS_MANAGER"
    TEAM_LEAD = "TEAM_LEAD"
    AGENT = "AGENT"
    CREDIT_SPECIALIST = "CREDIT_SPECIALIST"
    COLLECTIONS_SPECIALIST = "COLLECTIONS_SPECIALIST"
    AUDITOR = "AUDITOR"
    HEAD_OF_OPERATIONS = "HEAD_OF_OPERATIONS"


READ_ONLY: Final[frozenset[str]] = frozenset(
    {
        Permission.OPS_DASHBOARD_READ,
        Permission.OPS_USERS_READ,
        Permission.OPS_ROLES_READ,
        Permission.OPS_DEPARTMENTS_READ,
        Permission.OPS_QUEUES_READ,
        Permission.OPS_AVAILABILITY_READ,
        Permission.OPS_CASES_READ,
        Permission.OPS_COMMUNICATION_READ,
        Permission.OPS_REPORTS_READ,
        Permission.OPS_AUDIT_READ,
        Permission.OPS_INTEGRATION_READ,
    }
)

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    RoleKey.OPS_ADMIN: ALL_PERMISSIONS,
    RoleKey.OPS_MANAGER: frozenset(
        READ_ONLY
        | {
            Permission.OPS_USERS_WRITE,
            Permission.OPS_QUEUES_WRITE,
            Permission.OPS_DEPARTMENTS_WRITE,
            Permission.OPS_AVAILABILITY_WRITE,
            Permission.OPS_AVAILABILITY_APPROVE,
            Permission.OPS_CASES_CREATE,
            Permission.OPS_CASES_ASSIGN,
            Permission.OPS_CASES_LOCK,
            Permission.OPS_CASES_TRANSFER,
            Permission.OPS_CASES_CLOSE,
            Permission.OPS_CASES_COMMENT,
            Permission.OPS_COMMUNICATION_WRITE,
            Permission.OPS_EXPORT_REQUEST,
            Permission.OPS_SETTINGS_WRITE,
        }
    ),
    RoleKey.TEAM_LEAD: frozenset(
        READ_ONLY
        | {
            Permission.OPS_AVAILABILITY_WRITE,
            Permission.OPS_AVAILABILITY_APPROVE,
            Permission.OPS_CASES_ASSIGN,
            Permission.OPS_CASES_LOCK,
            Permission.OPS_CASES_TRANSFER,
            Permission.OPS_CASES_CLOSE,
            Permission.OPS_CASES_COMMENT,
            Permission.OPS_COMMUNICATION_WRITE,
        }
    ),
    RoleKey.AGENT: frozenset(
        {
            Permission.OPS_DASHBOARD_READ,
            Permission.OPS_QUEUES_READ,
            Permission.OPS_AVAILABILITY_READ,
            Permission.OPS_AVAILABILITY_WRITE,
            Permission.OPS_CASES_READ,
            Permission.OPS_CASES_LOCK,
            Permission.OPS_CASES_TRANSFER,
            Permission.OPS_CASES_COMMENT,
            Permission.OPS_COMMUNICATION_READ,
            Permission.OPS_COMMUNICATION_WRITE,
        }
    ),
    RoleKey.CREDIT_SPECIALIST: frozenset(
        {
            Permission.OPS_DASHBOARD_READ,
            Permission.OPS_QUEUES_READ,
            Permission.OPS_AVAILABILITY_READ,
            Permission.OPS_AVAILABILITY_WRITE,
            Permission.OPS_CASES_READ,
            Permission.OPS_CASES_LOCK,
            Permission.OPS_CASES_TRANSFER,
            Permission.OPS_CASES_COMMENT,
            Permission.OPS_COMMUNICATION_READ,
            Permission.OPS_COMMUNICATION_WRITE,
        }
    ),
    RoleKey.COLLECTIONS_SPECIALIST: frozenset(
        {
            Permission.OPS_DASHBOARD_READ,
            Permission.OPS_QUEUES_READ,
            Permission.OPS_AVAILABILITY_READ,
            Permission.OPS_AVAILABILITY_WRITE,
            Permission.OPS_CASES_READ,
            Permission.OPS_CASES_LOCK,
            Permission.OPS_CASES_TRANSFER,
            Permission.OPS_CASES_COMMENT,
            Permission.OPS_COMMUNICATION_READ,
            Permission.OPS_COMMUNICATION_WRITE,
        }
    ),
    RoleKey.AUDITOR: READ_ONLY,
    RoleKey.HEAD_OF_OPERATIONS: READ_ONLY | {Permission.OPS_EXPORT_REQUEST},
}

ROLE_LABELS: Final[dict[str, str]] = {
    RoleKey.OPS_ADMIN: "Ops Administrator",
    RoleKey.OPS_MANAGER: "Ops Manager",
    RoleKey.TEAM_LEAD: "Team Lead",
    RoleKey.AGENT: "Agent / Officer",
    RoleKey.CREDIT_SPECIALIST: "Credit Specialist",
    RoleKey.COLLECTIONS_SPECIALIST: "Collections Specialist",
    RoleKey.AUDITOR: "Auditor / Compliance",
    RoleKey.HEAD_OF_OPERATIONS: "Head of Operations",
}


def permissions_for_roles(roles: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Union the permissions granted by the supplied operational roles."""
    granted: set[str] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def is_known_permission(value: str) -> bool:
    return value in ALL_PERMISSIONS
