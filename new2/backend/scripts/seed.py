"""Seed the Ops Control Center with a working reference dataset.

Idempotent: running it twice changes nothing the second time. Safe to run
against a development or staging database; it refuses to touch production.

    python -m scripts.seed             # seed OPS_TENANT_ID
    python -m scripts.seed --tenant X  # seed another tenant (isolation testing)
    python -m scripts.seed --reset     # delete this tenant's rows first

What it creates
---------------
* Departments: CC, LOS, RO, CREDIT, OPS, COLLECTIONS
* SLA policies and their per-queue assignments
* Queues: LOS_RECOVERY, LMS_SERVICING, CREDIT_CALLS, COLLECTIONS
* Personas matching ``scripts/local_auth_users.json`` with roles attached
* A handful of work items so the workbench is not empty on first login

No password or credential material is written. The seeder reads only the
identity projection fields from the local auth file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models.org import (  # noqa: E402
    Department,
    DepartmentMembership,
    OrgStatus,
    Queue,
    QueueMembership,
    SourceApp,
)
from app.models.sla import (  # noqa: E402
    QueueSlaPolicy,
    SlaBreachAction,
    SlaPolicy,
    SlaStatus,
)
from app.models.user import OperationalStatus, User, UserRole  # noqa: E402
from app.models.work import Priority, WorkItem, WorkStatus  # noqa: E402
from app.security.permissions import RoleKey  # noqa: E402

logger = logging.getLogger("scripts.seed")

# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------
DEPARTMENTS: tuple[tuple[str, str, SourceApp, str], ...] = (
    ("CC", "Customer Care", SourceApp.OPS, "Front line customer contact."),
    ("LOS", "Loan Origination Support", SourceApp.LOS, "Cases originating from LOS."),
    ("LMS", "Loan Management Servicing", SourceApp.LMS, "Post disbursement servicing."),
    ("RO", "Recovery Operations", SourceApp.LOS, "Collections and recovery cases."),
    ("CREDIT", "Credit Desk", SourceApp.LMS, "Credit review and verification."),
    ("OPS", "Operations", SourceApp.OPS, "Cross functional operations."),
    ("COLLECTIONS", "Collections", SourceApp.LMS, "Outbound collections."),
)

SLA_POLICIES: tuple[tuple[str, str, SourceApp, int, int, SlaBreachAction, str], ...] = (
    (
        "STD_24H",
        "Standard - 24 hours",
        SourceApp.OPS,
        1440,
        240,
        SlaBreachAction.NOTIFY_MANAGER,
        "Default target for most queues.",
    ),
    (
        "URGENT_4H",
        "Urgent - 4 hours",
        SourceApp.OPS,
        240,
        60,
        SlaBreachAction.ESCALATE,
        "Time critical customer commitments.",
    ),
    (
        "RECOVERY_72H",
        "Recovery - 72 hours",
        SourceApp.LOS,
        4320,
        720,
        SlaBreachAction.NOTIFY_MANAGER,
        "Recovery and collections work.",
    ),
)

# (code, name, department_code, sla_code, priority, capacity, roles, specialisations)
QUEUES: tuple[tuple[str, str, str, str, int, int, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "LOS_RECOVERY",
        "LOS Recovery Desk",
        "LOS",
        "RECOVERY_72H",
        200,
        25,
        (),
        (),
    ),
    (
        "LMS_SERVICING",
        "LMS Servicing Desk",
        "LMS",
        "STD_24H",
        150,
        25,
        (),
        (),
    ),
    (
        "CREDIT_CALLS",
        "Credit Review Calls",
        "CREDIT",
        "URGENT_4H",
        300,
        15,
        ("CREDIT_SPECIALIST", "TEAM_LEAD"),
        ("credit review", "underwriting"),
    ),
    (
        "COLLECTIONS",
        "Collections Outreach",
        "COLLECTIONS",
        "RECOVERY_72H",
        100,
        30,
        ("COLLECTIONS_SPECIALIST",),
        ("collections",),
    ),
)

#: Persona -> (department, queues, extra roles, capacity, priority weight, specialisations)
#:
#: The specialisation list is not cosmetic. A queue carrying
#: ``required_specialisations`` rejects every candidate whose own
#: specialisation set does not intersect it, so without this CREDIT_CALLS and
#: COLLECTIONS would have no eligible user at all and auto-assign would always
#: answer NO_ELIGIBLE_USER. Values must match ``QUEUES`` above.
PERSONA_PLACEMENT: dict[
    str, tuple[str, tuple[str, ...], tuple[RoleKey, ...], int, int, tuple[str, ...]]
] = {
    "admin": ("OPS", ("LOS_RECOVERY", "LMS_SERVICING"), (RoleKey.OPS_ADMIN,), 10, 0, ()),
    "manager": (
        "OPS",
        ("LOS_RECOVERY", "LMS_SERVICING", "CREDIT_CALLS"),
        (RoleKey.OPS_MANAGER,),
        15,
        90,
        ("credit review",),
    ),
    "lead": ("CC", ("LOS_RECOVERY", "LMS_SERVICING"), (RoleKey.TEAM_LEAD,), 20, 70, ()),
    "agent": ("CC", ("LOS_RECOVERY",), (RoleKey.AGENT,), 15, 20, ()),
    "credit": (
        "CREDIT",
        ("CREDIT_CALLS",),
        (RoleKey.CREDIT_SPECIALIST,),
        10,
        40,
        ("credit review", "underwriting"),
    ),
    "collections": (
        "COLLECTIONS",
        ("COLLECTIONS",),
        (RoleKey.COLLECTIONS_SPECIALIST,),
        25,
        20,
        ("collections",),
    ),
    "auditor": ("OPS", (), (RoleKey.AUDITOR,), 5, 0, ()),
    "head": ("OPS", (), (RoleKey.HEAD_OF_OPERATIONS,), 5, 0, ()),
    "suspended": ("CC", (), (RoleKey.AGENT,), 10, 0, ()),
}

WORK_ITEMS: tuple[tuple[str, str, str, Priority, str], ...] = (
    (
        "LOS-2026-0001",
        "LOS_RECOVERY",
        "Loan disbursement query - application 4471",
        Priority.HIGH,
        "Meera R",
    ),
    (
        "LOS-2026-0002",
        "LOS_RECOVERY",
        "Repayment schedule mismatch - application 4472",
        Priority.MEDIUM,
        "Nikhil S",
    ),
    (
        "LMS-2026-0003",
        "LMS_SERVICING",
        "EMI debit failed - account 8890",
        Priority.URGENT,
        "Priya K",
    ),
    (
        "LMS-2026-0004",
        "LMS_SERVICING",
        "Statement request - account 8891",
        Priority.LOW,
        "Rahul V",
    ),
    (
        "CRD-2026-0005",
        "CREDIT_CALLS",
        "Income verification pending - application 5512",
        Priority.HIGH,
        "Sana A",
    ),
    (
        "COL-2026-0006",
        "COLLECTIONS",
        "Overdue 30 days - account 7734",
        Priority.MEDIUM,
        "Vikram D",
    ),
)


# ---------------------------------------------------------------------------
def seed(session: Session, *, tenant_id: str, local_auth_users: Path) -> dict[str, int]:
    counts: dict[str, int] = {}

    departments: dict[str, Department] = {}
    for code, name, source_app, description in DEPARTMENTS:
        department = session.scalar(
            select(Department).where(Department.tenant_id == tenant_id, Department.code == code)
        )
        if department is None:
            department = Department(
                tenant_id=tenant_id,
                code=code,
                name=name,
                source_app=source_app,
                status=OrgStatus.ACTIVE,
                description=description,
            )
            session.add(department)
            session.flush()
        departments[code] = department
    counts["departments"] = len(departments)

    policies: dict[str, SlaPolicy] = {}
    for code, name, source_app, target, warning, action, description in SLA_POLICIES:
        policy = session.scalar(
            select(SlaPolicy).where(SlaPolicy.tenant_id == tenant_id, SlaPolicy.code == code)
        )
        if policy is None:
            policy = SlaPolicy(
                tenant_id=tenant_id,
                code=code,
                name=name,
                source_app=source_app,
                target_minutes=target,
                warning_minutes=warning,
                business_hours_only=False,
                breach_action=action,
                is_active=True,
                description=description,
            )
            session.add(policy)
            session.flush()
        policies[code] = policy
    counts["sla_policies"] = len(policies)

    queues: dict[str, Queue] = {}
    for code, name, dept, sla, priority, capacity, roles, specialisations in QUEUES:
        queue = session.scalar(
            select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == code)
        )
        if queue is None:
            queue = Queue(
                tenant_id=tenant_id,
                code=code,
                name=name,
                source_app=departments[dept].source_app,
                department_id=departments[dept].id,
                status=OrgStatus.ACTIVE,
                description=f"{name} ({dept})",
                max_workload_per_user=capacity,
                priority=priority,
                sla_policy_id=policies[sla].id,
                eligible_roles=list(roles) or None,
                required_specialisations=list(specialisations) or None,
                sync_interval_minutes=15,
            )
            session.add(queue)
            session.flush()
            queue_sla = session.scalar(
                select(QueueSlaPolicy).where(
                    QueueSlaPolicy.tenant_id == tenant_id,
                    QueueSlaPolicy.queue_id == queue.id,
                    QueueSlaPolicy.sla_policy_id == policies[sla].id,
                )
            )
            if queue_sla is None:
                session.add(
                    QueueSlaPolicy(
                        tenant_id=tenant_id,
                        queue_id=queue.id,
                        sla_policy_id=policies[sla].id,
                        is_active=True,
                        is_default_for_queue=True,
                    )
                )
        queues[code] = queue
    counts["queues"] = len(queues)

    counts["users"] = _seed_users(
        session,
        tenant_id=tenant_id,
        departments=departments,
        queues=queues,
        source=local_auth_users,
    )
    counts["work_items"] = _seed_work_items(session, tenant_id=tenant_id, policies=policies)

    session.flush()
    return counts


def _seed_users(
    session: Session,
    *,
    tenant_id: str,
    departments: dict[str, Department],
    queues: dict[str, Queue],
    source: Path,
) -> int:
    entries = _local_auth_entries(source)
    created = 0
    for persona, entry in entries.items():
        username = str(entry["username"])
        (
            department_code,
            queue_codes,
            extra_roles,
            capacity,
            priority_weight,
            specialisations,
        ) = PERSONA_PLACEMENT.get(persona, ("OPS", (), (), 10, 0, ()))

        user = session.scalar(
            select(User).where(User.tenant_id == tenant_id, User.username == username)
        )
        if user is None:
            user = User(
                tenant_id=tenant_id,
                username=username,
                display_name=str(entry.get("display_name") or username),
                email=entry.get("email"),
                bre_user_id=str(entry.get("bre_user_id") or f"local-{username}"),
                status=OperationalStatus.ACTIVE
                if entry.get("active", True)
                else OperationalStatus.INACTIVE,
                is_available=True,
                capacity=capacity,
                priority_weight=priority_weight,
                daily_allocation_limit=max(capacity * 4, 20),
                job_title=_job_title(persona),
                # Stored masked; the raw number is never persisted or returned.
                phone_masked=_masked_phone(entry.get("phone")),
                # CSV, not JSON: every reader splits this column on "," (see
                # User.bre_role_list and the user/auth services). json.dumps
                # here produced a single element '["OPS_MANAGER"]' that the API
                # then served back verbatim.
                bre_roles=",".join(str(role) for role in (entry.get("roles") or [])) or None,
                # Same CSV convention as bre_roles / specialisation_list.
                specialisations=",".join(specialisations) or None,
            )
            session.add(user)
            session.flush()
            created += 1

        persona_roles: set[str] = {str(role) for role in (entry.get("roles") or [])}
        persona_roles.update(role.value for role in extra_roles)
        for role in persona_roles:
            existing = session.scalar(
                select(UserRole).where(
                    UserRole.tenant_id == tenant_id,
                    UserRole.user_id == user.id,
                    UserRole.role == role,
                )
            )
            if existing is None:
                session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role))

        membership = session.scalar(
            select(DepartmentMembership).where(
                DepartmentMembership.tenant_id == tenant_id,
                DepartmentMembership.department_id == departments[department_code].id,
                DepartmentMembership.user_id == user.id,
            )
        )
        if membership is None:
            session.add(
                DepartmentMembership(
                    tenant_id=tenant_id,
                    department_id=departments[department_code].id,
                    user_id=user.id,
                    is_primary=True,
                    is_active=True,
                )
            )

        for queue_code in queue_codes:
            queue = queues[queue_code]
            # Staffing a queue implies belonging to that queue's department.
            # Allocation rejects a candidate whose department set does not
            # contain the queue's department (see services/allocation.py), so
            # without this every persona lands in its home department only and
            # LOS/LMS queues end up with zero eligible users.
            if (
                queue.department_id is not None
                and queue.department_id != departments[department_code].id
            ):
                queue_department = session.scalar(
                    select(DepartmentMembership).where(
                        DepartmentMembership.tenant_id == tenant_id,
                        DepartmentMembership.department_id == queue.department_id,
                        DepartmentMembership.user_id == user.id,
                    )
                )
                if queue_department is None:
                    session.add(
                        DepartmentMembership(
                            tenant_id=tenant_id,
                            department_id=queue.department_id,
                            user_id=user.id,
                            is_primary=False,
                            is_active=True,
                        )
                    )

            queue_membership = session.scalar(
                select(QueueMembership).where(
                    QueueMembership.tenant_id == tenant_id,
                    QueueMembership.queue_id == queue.id,
                    QueueMembership.user_id == user.id,
                )
            )
            if queue_membership is None:
                # Pin the specialisation at the membership level too: it is the
                # per-queue override allocation consults, and it is what makes
                # a specialist's tag visible on the queue rather than only on
                # the person.
                required = queue.required_specialisations or []
                membership_specialisation = next(
                    (tag for tag in specialisations if tag in required),
                    None,
                )
                session.add(
                    QueueMembership(
                        tenant_id=tenant_id,
                        queue_id=queue.id,
                        user_id=user.id,
                        is_active=True,
                        capacity=None,
                        priority_weight=priority_weight,
                        specialisation=membership_specialisation,
                        shift_start="09:00",
                        shift_end="18:00",
                    )
                )
    session.flush()
    return len(entries)


def _source_app_for(queue_code: str) -> SourceApp:
    """Map a queue to the system of record its cases originate from."""
    if queue_code.startswith("LOS"):
        return SourceApp.LOS
    return SourceApp.LMS


def _seed_work_items(session: Session, *, tenant_id: str, policies: dict[str, SlaPolicy]) -> int:
    now = datetime.now(UTC)
    created = 0
    for index, (reference, queue_code, title, priority, customer) in enumerate(WORK_ITEMS):
        existing = session.scalar(
            select(WorkItem).where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.source_app == _source_app_for(queue_code),
                WorkItem.source_key == reference,
            )
        )
        if existing is not None:
            continue
        policy = policies["URGENT_4H" if priority is Priority.URGENT else "STD_24H"]
        created_at = now - timedelta(hours=index * 3)
        session.add(
            WorkItem(
                tenant_id=tenant_id,
                source_app=_source_app_for(queue_code),
                source_key=reference,
                external_ref=reference,
                title=title,
                customer_name=customer,
                customer_mobile_masked=_masked_phone("98" + str(100000000 + index * 1234)),
                queue_code=queue_code,
                status=WorkStatus.OPEN,
                priority=priority,
                sla_policy_id=policy.id,
                sla_status=SlaStatus.NOT_STARTED,
                due_at=created_at + timedelta(minutes=policy.target_minutes),
                last_activity_at=created_at,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        created += 1
    session.flush()
    return created


# ---------------------------------------------------------------------------
def _local_auth_entries(source: Path) -> dict[str, dict[str, Any]]:
    """Read the persona file, falling back to the personas above."""
    fallback: dict[str, dict[str, Any]] = {
        username: {
            "username": username,
            "display_name": username.title(),
            "roles": [role.value for role in placement[2]],
        }
        for username, placement in PERSONA_PLACEMENT.items()
    }
    if not source.exists():
        logger.warning("local_auth_users_missing path=%s using built-in personas", source)
        return fallback
    data: Any = json.loads(source.read_text(encoding="utf-8"))
    users = data.get("users") if isinstance(data, dict) else data
    entries: dict[str, dict[str, Any]] = {}
    for entry in users or []:
        if not isinstance(entry, dict):
            continue
        persona = str(entry.get("username") or "")
        if persona:
            entries[persona] = entry
    return entries or fallback


def _job_title(persona: str) -> str:
    return {
        "admin": "Ops Platform Administrator",
        "manager": "Operations Manager",
        "lead": "Team Lead - Customer Care",
        "agent": "Customer Care Officer",
        "credit": "Credit Specialist",
        "collections": "Collections Specialist",
        "auditor": "Compliance Auditor",
        "head": "Head of Operations",
        "suspended": "Former Officer",
    }.get(persona, "Officer")


def _masked_phone(raw: object) -> str | None:
    """Never store a raw number. Sample data only; see app.security.masking."""
    from app.security.masking import mask_mobile

    return mask_mobile(str(raw)) if raw else None


def _reset(session: Session, tenant_id: str) -> None:
    """Remove every seeded table for one tenant, children first."""
    from app.models.audit import AuditLog
    from app.models.availability import UserAvailability
    from app.models.communication import CommunicationEvent
    from app.models.integration import IntegrationHealth

    for model in (
        CommunicationEvent,
        AuditLog,
        UserAvailability,
        WorkItem,
        QueueSlaPolicy,
        QueueMembership,
        DepartmentMembership,
        UserRole,
        Queue,
        SlaPolicy,
        Department,
        User,
        IntegrationHealth,
    ):
        session.execute(delete(model).where(model.tenant_id == tenant_id))
    session.commit()
    logger.info("reset_complete tenant=%s", tenant_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=settings.ops_default_tenant)
    parser.add_argument(
        "--reset", action="store_true", help="delete this tenant's rows before seeding"
    )
    parser.add_argument(
        "--local-auth-users",
        type=Path,
        default=settings.ops_local_auth_users_json,
        help="persona file to provision users from",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-5s %(message)s",
    )

    if settings.is_production:
        logger.error("refusing_to_seed production")
        return 2

    with session_scope() as session:
        if args.reset:
            _reset(session, args.tenant)
        counts = seed(
            session,
            tenant_id=args.tenant,
            local_auth_users=args.local_auth_users,
        )

    # A single mapping, consistently. Mixing ``%s`` positional with
    # ``%(name)s`` makes logging pass a tuple plus a dict to ``%``, which
    # raises "format requires a mapping" and hides an otherwise clean run.
    logger.info(
        "seeded tenant=%(tenant)s departments=%(departments)d "
        "sla_policies=%(sla_policies)d queues=%(queues)d users=%(users)d "
        "new_work_items=%(work_items)d",
        {"tenant": args.tenant, **counts},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
