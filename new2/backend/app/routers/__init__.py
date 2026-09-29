"""HTTP routers.

Registration order in :mod:`app.main` is deliberate: ``/auth`` first (it is the
only unauthenticated surface), then reference data, then the workbench, then
the read-only reporting surfaces.

Every router is mounted under ``settings.api_prefix`` (``/api``). Routers never
perform their own permission checks - they declare the requirement through
:func:`app.dependencies.auth.require_permissions` and the dependency performs
the check (PRD Rule 2).
"""

from __future__ import annotations

from app.routers import (
    audit,
    auth,
    availability,
    communications,
    departments,
    integrations,
    queues,
    stats,
    users,
    work_items,
)

#: Routers registered in order. Several modules expose more than one router
#: (``departments.memberships_router``, ``queues.memberships_router`` and
#: ``integrations.sync_runs_router``) because they own distinct URL spaces.
ALL_ROUTERS = (
    auth.router,
    users.router,
    departments.router,
    departments.memberships_router,
    queues.router,
    queues.memberships_router,
    work_items.router,
    availability.router,
    communications.router,
    stats.router,
    audit.router,
    integrations.router,
    integrations.sync_runs_router,
)

__all__ = [
    "ALL_ROUTERS",
    "audit",
    "auth",
    "availability",
    "communications",
    "departments",
    "integrations",
    "queues",
    "stats",
    "users",
    "work_items",
]
