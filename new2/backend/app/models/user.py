"""Users and role assignments.

BRE owns identity. ``ops_users`` stores only the operational projection plus a
pointer to the BRE identity plus the permissions Ops resolved for the session.
No password and no credential material is ever stored (PRD Rule 1, section 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OperationalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


class AvailabilityStatus(StrEnum):
    """Coarse availability state derived from active windows."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    ON_LEAVE = "ON_LEAVE"


class UserRole(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """An Ops operational role held by a user.

    Roles are not identity roles. They describe how a person behaves in Ops.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "role", name="uq_user_roles_tenant_user_role"),
        Index("ix_user_roles_tenant_role", "tenant_id", "role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)

    user: Mapped[User] = relationship(back_populates="roles")


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """Operational projection of a BRE identity."""

    __tablename__ = "ops_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "bre_user_id", name="uq_ops_users_tenant_bre_user"),
        UniqueConstraint("tenant_id", "username", name="uq_ops_users_tenant_username"),
        Index("ix_ops_users_tenant_status", "tenant_id", "status"),
        Index("ix_ops_users_tenant_display_name", "tenant_id", "display_name"),
    )

    # --- identity projection (BRE is authoritative) ----------------------
    bre_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # --- operational metadata (Ops owned) --------------------------------
    status: Mapped[OperationalStatus] = mapped_column(
        Enum(OperationalStatus, name="operational_status", native_enum=True),
        nullable=False,
        default=OperationalStatus.ACTIVE,
    )
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: End of the blocking availability window currently in force, if any.
    availability_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    priority_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_allocation_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    job_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    specialisations: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- BRE passthrough metadata ---------------------------------------
    bre_roles: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def role_keys(self) -> list[str]:
        return sorted(role.role for role in self.roles)

    @property
    def specialisation_list(self) -> list[str]:
        if not self.specialisations:
            return []
        return [item.strip() for item in self.specialisations.split(",") if item.strip()]
