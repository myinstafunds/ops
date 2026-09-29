"""BRE Identity integration (users, roles, MFA)."""

from app.integrations.bre.client import (
    BreAuthResult,
    BreIdentityProvider,
    BreUser,
    IdentityProvider,
    get_provider,
)

__all__ = [
    "BreAuthResult",
    "BreIdentityProvider",
    "BreUser",
    "IdentityProvider",
    "get_provider",
]
