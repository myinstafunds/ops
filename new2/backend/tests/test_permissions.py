"""Permission registry integrity (PRD 11, Appendix C).

The registry is the single source of truth for the access surface, so these
tests guard its *shape* as much as its content: no permission may be granted by
a role without being declared, and an agent must never hold an admin capability.
"""

from __future__ import annotations

import pytest

from app.security.permissions import (
    ALL_PERMISSIONS,
    READ_ONLY,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    Permission,
    RoleKey,
    is_known_permission,
    permissions_for_roles,
)


def test_every_declared_permission_is_unique() -> None:
    values = [permission.value for permission in Permission]
    assert len(values) == len(set(values))


def test_role_permission_sets_only_reference_declared_permissions() -> None:
    for role, granted in ROLE_PERMISSIONS.items():
        unknown = granted - ALL_PERMISSIONS
        assert not unknown, f"role {role} grants undeclared permissions: {unknown}"


def test_every_role_has_a_label() -> None:
    assert set(ROLE_LABELS) == set(ROLE_PERMISSIONS)


def test_admin_holds_the_complete_surface() -> None:
    assert ROLE_PERMISSIONS[RoleKey.OPS_ADMIN] == ALL_PERMISSIONS


def test_read_only_roles_hold_no_write_permission() -> None:
    write_permissions = {
        Permission.OPS_USERS_WRITE,
        Permission.OPS_ROLES_WRITE,
        Permission.OPS_DEPARTMENTS_WRITE,
        Permission.OPS_QUEUES_WRITE,
        Permission.OPS_AVAILABILITY_WRITE,
        Permission.OPS_AVAILABILITY_APPROVE,
        Permission.OPS_CASES_CREATE,
        Permission.OPS_CASES_ASSIGN,
        Permission.OPS_CASES_CLOSE,
        Permission.OPS_SETTINGS_WRITE,
    }
    for role in (RoleKey.AUDITOR, RoleKey.HEAD_OF_OPERATIONS):
        assert not ROLE_PERMISSIONS[role] & write_permissions, f"{role} must be read only"


def test_auditor_holds_exactly_the_read_only_set() -> None:
    assert ROLE_PERMISSIONS[RoleKey.AUDITOR] == READ_ONLY


@pytest.mark.parametrize(
    "role", [RoleKey.AGENT, RoleKey.CREDIT_SPECIALIST, RoleKey.COLLECTIONS_SPECIALIST]
)
def test_agent_like_roles_cannot_close_or_approve(role: RoleKey) -> None:
    granted = ROLE_PERMISSIONS[role]
    assert Permission.OPS_CASES_CLOSE not in granted
    assert Permission.OPS_AVAILABILITY_APPROVE not in granted
    assert Permission.OPS_SETTINGS_WRITE not in granted


def test_agent_can_work_a_case_but_not_create_one() -> None:
    granted = permissions_for_roles([RoleKey.AGENT])
    assert Permission.OPS_CASES_LOCK in granted
    assert Permission.OPS_CASES_COMMENT in granted
    assert Permission.OPS_CASES_CREATE not in granted


def test_permissions_for_roles_unions_multiple_roles() -> None:
    combined = permissions_for_roles([RoleKey.AGENT, RoleKey.AUDITOR])
    assert Permission.OPS_CASES_LOCK in combined
    assert Permission.OPS_AUDIT_READ in combined


def test_unknown_role_grants_nothing_rather_than_everything() -> None:
    assert permissions_for_roles(["NOT_A_ROLE"]) == frozenset()


def test_is_known_permission() -> None:
    assert is_known_permission(Permission.OPS_AUDIT_READ)
    assert not is_known_permission("OPS_MADE_UP")


def test_writeback_permissions_exist_but_are_admin_only() -> None:
    """Release 7 surface is declared, gated by the feature flag, not shipped."""
    for permission in (Permission.OPS_LOS_WRITEBACK, Permission.OPS_LMS_WRITEBACK):
        holders = [role for role, granted in ROLE_PERMISSIONS.items() if permission in granted]
        assert holders == [RoleKey.OPS_ADMIN]
