"""Local development identity provider.

Enabled only when ``OPS_AUTH_MODE=local``. The settings validator refuses this
mode in staging and production, so shipping it by accident is not possible.

Credentials live in a JSON file that is git-ignored. Passwords are compared
with ``hmac.compare_digest`` and are never logged.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import AuthenticationError
from app.integrations.bre.client import BreAuthResult, BreUser
from app.security.permissions import RoleKey

logger = logging.getLogger(__name__)

#: Mappings from a friendly persona name to the Ops roles it grants.
PERSONA_ROLES: dict[str, list[str]] = {
    "admin": [RoleKey.OPS_ADMIN.value],
    "manager": [RoleKey.OPS_MANAGER.value],
    "teamlead": [RoleKey.TEAM_LEAD.value],
    "agent": [RoleKey.AGENT.value],
    "credit": [RoleKey.CREDIT_SPECIALIST.value],
    "collections": [RoleKey.COLLECTIONS_SPECIALIST.value],
    "auditor": [RoleKey.AUDITOR.value],
    "head": [RoleKey.HEAD_OF_OPERATIONS.value],
}


class LocalIdentityProvider:
    """File-backed stand-in for BRE."""

    name = "local"

    def __init__(self, users_path: Path | None = None) -> None:
        self.users_path = users_path or settings.ops_local_auth_users_json

    def authenticate(self, username: str, password: str) -> BreAuthResult:
        users = self._load()
        entry = next(
            (item for item in users if item.get("username", "").lower() == username.lower()),
            None,
        )
        if entry is None:
            # Same message and roughly the same work as a wrong password so the
            # endpoint does not confirm which usernames exist.
            _burn_cycles()
            raise AuthenticationError("The username or password is incorrect.")
        expected = str(entry.get("password", ""))
        if not hmac.compare_digest(expected, password):
            _burn_cycles()
            raise AuthenticationError("The username or password is incorrect.")
        if not entry.get("active", True):
            raise AuthenticationError("This account has been deactivated.")

        user = BreUser(
            bre_user_id=str(entry.get("bre_user_id") or f"local-{entry['username']}"),
            username=str(entry["username"]),
            display_name=str(entry.get("display_name") or entry["username"]),
            email=entry.get("email"),
            roles=tuple(entry.get("roles") or []),
            tenant_id=str(entry.get("tenant_id") or settings.ops_tenant_id),
            last_login_at=datetime.now(UTC).isoformat(),
            mfa_required=bool(entry.get("mfa_required", False)),
        )
        return BreAuthResult(
            user=user,
            mfa_token="local-mfa" if user.mfa_required else None,
            mfa_required=user.mfa_required,
        )

    def verify_mfa(self, mfa_token: str, code: str) -> BreAuthResult:
        users = self._load()
        # The local MFA token is opaque on purpose; resolve the user by username
        # carried alongside it so no session store is needed in development.
        username = mfa_token.removeprefix("local-mfa:")
        entry = next(
            (item for item in users if item.get("username", "").lower() == username.lower()),
            None,
        )
        if entry is None:
            raise AuthenticationError("That verification session has expired. Sign in again.")
        if not hmac.compare_digest(settings.ops_local_auth_mfa_code, code):
            raise AuthenticationError("That verification code is not correct.")
        user = BreUser(
            bre_user_id=str(entry.get("bre_user_id") or f"local-{entry['username']}"),
            username=str(entry["username"]),
            display_name=str(entry.get("display_name") or entry["username"]),
            email=entry.get("email"),
            roles=tuple(entry.get("roles") or []),
            tenant_id=str(entry.get("tenant_id") or settings.ops_tenant_id),
        )
        return BreAuthResult(user=user)

    def fetch_user(self, bre_user_id: str) -> BreUser | None:
        users = self._load()
        entry = next(
            (
                item
                for item in users
                if str(item.get("bre_user_id") or f"local-{item['username']}") == bre_user_id
            ),
            None,
        )
        if entry is None:
            return None
        return BreUser(
            bre_user_id=bre_user_id,
            username=str(entry["username"]),
            display_name=str(entry.get("display_name") or entry["username"]),
            email=entry.get("email"),
            roles=tuple(entry.get("roles") or []),
            tenant_id=str(entry.get("tenant_id") or settings.ops_tenant_id),
        )

    def mfa_token_for(self, username: str) -> str:
        return f"local-mfa:{username}"

    def _load(self) -> list[dict[str, Any]]:
        if not settings.is_development and settings.ops_auth_mode != "local":
            raise AuthenticationError("Local authentication is not available in this environment.")
        if not self.users_path.exists():
            logger.warning("local_auth_users_missing path=%s", self.users_path)
            return []
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("local_auth_users_unreadable path=%s", self.users_path)
            raise AuthenticationError(
                "Local authentication is misconfigured. Contact an administrator."
            ) from exc
        users = data.get("users") if isinstance(data, dict) else data
        return users if isinstance(users, list) else []


def _burn_cycles() -> None:
    """Constant-ish work on the failure path to blunt timing attacks."""
    digest = b"0"
    for char in b"ops-control-centre":
        digest = hmac.new(digest, bytes([char]), "sha256").digest()


__all__ = ["PERSONA_ROLES", "LocalIdentityProvider"]
