"""BRE Identity adapter (PRD 4, 7, Rule 1).

BRE is the identity source. This module is the only place Ops talks to it, and
it is written so that a BRE outage produces a clean 502/504 without touching
Ops' own data or blocking anything else.

Two providers implement :class:`IdentityProvider`:

* :class:`BreIdentityProvider` - the real integration.
* :class:`LocalIdentityProvider` - development only, refused in staging and
  production by the settings validator.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import settings
from app.errors import (
    AuthenticationError,
    RateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BreUser:
    """Identity projection returned by BRE. No password ever crosses here."""

    bre_user_id: str
    username: str
    display_name: str
    email: str | None
    roles: tuple[str, ...]
    tenant_id: str
    last_login_at: str | None = None
    mfa_required: bool = False


@dataclass(frozen=True, slots=True)
class BreAuthResult:
    user: BreUser
    #: Short lived proof that credentials were accepted but MFA is outstanding.
    mfa_token: str | None = None
    mfa_required: bool = False


class IdentityProvider(Protocol):
    """Contract the auth router depends on, so providers are swappable."""

    name: str

    def authenticate(self, username: str, password: str) -> BreAuthResult: ...

    def verify_mfa(self, mfa_token: str, code: str) -> BreAuthResult: ...

    def fetch_user(self, bre_user_id: str) -> BreUser | None: ...


class BreIdentityProvider:
    """HTTP client for the BRE Identity service."""

    name = "bre"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        verify_tls: bool | None = None,
    ) -> None:
        self.base_url = (base_url or settings.ops_bre_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.ops_bre_timeout_seconds
        self.verify_tls = settings.ops_bre_verify_tls if verify_tls is None else verify_tls

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            verify=self.verify_tls,
            follow_redirects=False,
        )

    # -- authentication ----------------------------------------------------
    def authenticate(self, username: str, password: str) -> BreAuthResult:
        payload = self._post("/api/v1/auth/login", {"username": username, "password": password})
        mfa_required = bool(payload.get("mfa_required"))
        if mfa_required:
            return BreAuthResult(
                user=_parse_user(payload.get("user") or {}, username=username),
                mfa_token=payload.get("mfa_token"),
                mfa_required=True,
            )
        return BreAuthResult(user=_parse_user(payload.get("user") or {}, username=username))

    def verify_mfa(self, mfa_token: str, code: str) -> BreAuthResult:
        payload = self._post("/api/v1/auth/mfa/verify", {"mfa_token": mfa_token, "code": code})
        return BreAuthResult(user=_parse_user(payload.get("user") or {}))

    def fetch_user(self, bre_user_id: str) -> BreUser | None:
        try:
            payload = self._get(f"/api/v1/users/{bre_user_id}")
        except UpstreamUnavailableError:
            return None
        return _parse_user(payload)

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json_body=body)

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.request(method, path, json=json_body)
        except httpx.TimeoutException as exc:
            logger.warning("bre_timeout path=%s", path)
            raise UpstreamTimeoutError() from exc
        except httpx.HTTPError as exc:
            logger.warning("bre_unavailable path=%s error=%s", path, type(exc).__name__)
            raise UpstreamUnavailableError() from exc

        return self._handle(response)

    def _handle(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 401:
            raise AuthenticationError("The username or password is incorrect.")
        if response.status_code == 429:
            raise RateLimitedError()
        if response.status_code >= 500:
            logger.warning("bre_error status=%s", response.status_code)
            raise UpstreamUnavailableError()
        if response.status_code == 404:
            raise UpstreamUnavailableError("The identity service is not reachable.")
        if response.status_code >= 400:
            raise AuthenticationError(_safe_detail(response))
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("bre_bad_payload status=%s", response.status_code)
            raise UpstreamUnavailableError() from exc
        return payload if isinstance(payload, dict) else {}


def _safe_detail(response: httpx.Response) -> str:
    """Never echo an upstream body verbatim into a user facing message."""
    return "The identity service rejected the sign-in request."


def _parse_user(payload: dict[str, Any], username: str = "") -> BreUser:
    raw_username = str(payload.get("username") or username or "").strip()
    if not raw_username:
        raise UpstreamUnavailableError("The identity service returned an incomplete response.")
    return BreUser(
        bre_user_id=str(payload.get("id") or payload.get("user_id") or raw_username),
        username=raw_username,
        display_name=str(payload.get("display_name") or payload.get("name") or raw_username),
        email=payload.get("email"),
        roles=tuple(str(role) for role in (payload.get("roles") or [])),
        tenant_id=str(payload.get("tenant_id") or settings.ops_tenant_id),
        last_login_at=payload.get("last_login_at"),
        mfa_required=bool(payload.get("mfa_required")),
    )


def get_provider() -> IdentityProvider:
    """Provider chosen by configuration."""
    if settings.ops_auth_mode == "local":
        from app.integrations.bre.local_auth import LocalIdentityProvider

        return LocalIdentityProvider()
    return BreIdentityProvider()


__all__ = [
    "BreAuthResult",
    "BreIdentityProvider",
    "BreUser",
    "IdentityProvider",
    "get_provider",
]
