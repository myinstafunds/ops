"""Application configuration.

All settings are environment driven and validated at import time so that a
misconfigured deployment fails fast rather than failing on the first request.

Production rule (PRD 41): secrets are never read from source control and the
local development auth provider is refused outside development.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

AuthMode = Literal["bre", "local"]
EnvName = Literal["development", "test", "staging", "production"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    """Runtime settings for the Ops Control Center API."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core -------------------------------------------------------------
    ops_env: EnvName = "development"
    app_name: str = "Ops Control Center"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"

    # The tenant this deployment serves. Tenant scope is ALWAYS derived from
    # the authenticated identity; this value is only the fallback identity
    # claim and the seed value for local tooling. (PRD 40, Rule 4)
    ops_tenant_id: str = "BPSEC"
    ops_default_tenant: str = "BPSEC"

    # Declared as text rather than ``list[str]`` on purpose. pydantic-settings
    # JSON-decodes any complex-typed field straight out of the environment and
    # raises before a ``mode="before"`` validator ever runs, so a comma
    # separated value in ``.env`` would be unsatisfiable. The raw string is
    # parsed once, lazily, by :attr:`cors_origins`.
    ops_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- database ---------------------------------------------------------
    database_url: str = (
        "postgresql+psycopg://ops:change-me-locally@localhost:5432/ops_control_center"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # --- security ---------------------------------------------------------
    ops_jwt_secret: str = "dev-only-insecure-secret-replace-me"
    ops_jwt_algorithm: str = "HS256"
    ops_access_token_ttl_minutes: int = 60
    ops_refresh_token_ttl_minutes: int = 60 * 12
    ops_login_rate_limit_per_minute: int = 10
    ops_login_rate_limit_lockout_minutes: int = 15

    # --- request limits ---------------------------------------------------
    ops_max_body_bytes: int = 1_048_576
    ops_default_page_size: int = 25
    ops_max_page_size: int = 100

    # --- authentication ---------------------------------------------------
    ops_auth_mode: AuthMode = "bre"
    ops_bre_base_url: str = "https://bre-identity.example.internal"
    ops_bre_timeout_seconds: float = 10.0
    ops_bre_verify_tls: bool = True
    ops_bre_token_leeway_seconds: int = 30
    ops_bre_service_account_client_id: str | None = None
    ops_bre_service_account_client_secret: str | None = None

    ops_local_auth_enabled: bool = False
    ops_local_auth_users_json: Path = BACKEND_ROOT / "scripts" / "local_auth_users.json"
    ops_local_auth_mfa_code: str = "000000"

    # --- locking ----------------------------------------------------------
    ops_lock_ttl_minutes: int = 15
    ops_lock_heartbeat_seconds: int = 300

    # --- sla --------------------------------------------------------------
    ops_sla_due_soon_minutes: int = 60

    # --- feature flags (PRD 44) ------------------------------------------
    ops_feature_los_writeback: bool = False
    ops_feature_lms_writeback: bool = False
    ops_feature_writeback_kill_switch: bool = True

    # --- observability ----------------------------------------------------
    ops_log_level: str = "INFO"
    ops_log_format: LogFormat = "json"
    ops_log_sql: bool = False

    @field_validator("ops_log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _enforce_safety_invariants(self) -> Settings:
        """Refuse unsafe combinations outright instead of warning."""
        if self.ops_env == "production":
            if self.ops_feature_los_writeback or self.ops_feature_lms_writeback:
                # Write-back may only ever be switched on by a deliberate
                # deployment change after UAT sign-off. Keep it off here.
                object.__setattr__(self, "ops_feature_los_writeback", False)
                object.__setattr__(self, "ops_feature_lms_writeback", False)
            if self.ops_auth_mode != "bre":
                raise ValueError("ops_auth_mode must be 'bre' in production (PRD Rule 1)")
            if self.ops_local_auth_enabled:
                raise ValueError("ops_local_auth_enabled must be false in production")
            if "replace-me" in self.ops_jwt_secret or "insecure" in self.ops_jwt_secret:
                raise ValueError("ops_jwt_secret must be rotated before production")
        if self.ops_env in {"staging", "production"} and self.ops_auth_mode == "local":
            raise ValueError("local auth mode is forbidden outside development")
        if self.ops_default_page_size > self.ops_max_page_size:
            raise ValueError("ops_default_page_size cannot exceed ops_max_page_size")
        if self.ops_lock_heartbeat_seconds * 2 > self.ops_lock_ttl_minutes * 60:
            raise ValueError(
                "lock heartbeat interval must be comfortably shorter than the lock TTL"
            )
        return self

    # --- derived helpers --------------------------------------------------

    @property
    def cors_origins(self) -> list[str]:
        """The CORS allow-list.

        Accepts a JSON array or a comma separated list. Trailing slashes are
        stripped so ``http://localhost:5173/`` and ``http://localhost:5173``
        do not become two distinct origins, which browsers treat as different.
        """
        raw = self.ops_cors_origins.strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed = json.loads(raw)
            return [str(item).strip().rstrip("/") for item in parsed if str(item).strip()]
        return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.ops_env == "production"

    @property
    def is_development(self) -> bool:
        return self.ops_env == "development"

    @property
    def sync_database_url(self) -> str:
        """The URL Alembic and the seeder should connect with.

        ``psycopg`` 3 is driven synchronously, so the URL needs no rewriting:
        ``postgresql+psycopg://`` is already the correct sync entry point.
        Stripping the driver would be actively wrong, because bare
        ``postgresql://`` means *psycopg2* to SQLAlchemy, which is not
        installed and is not the driver this project targets.
        """
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


settings = get_settings()
