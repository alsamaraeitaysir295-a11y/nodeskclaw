"""User model."""

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class UserRole(str, Enum):
    admin = "admin"
    user = "user"


class User(BaseModel):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_username", "username",
            unique=True, postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_users_employee_id", "employee_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND employee_id IS NOT NULL"),
        ),
        Index(
            "uq_users_email", "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND email IS NOT NULL"),
        ),
        Index(
            "uq_users_phone", "phone",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND phone IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    employee_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.user, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 软删时记录是哪个超管删除的（仅 users 表特有；其他表本期不引入）
    deleted_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SaaS 多租户字段
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    current_org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True
    )

    # relationships
    clusters = relationship("Cluster", back_populates="creator", foreign_keys="Cluster.created_by")
    instances = relationship("Instance", back_populates="creator", foreign_keys="Instance.created_by")
    current_org = relationship("Organization", foreign_keys=[current_org_id])
    memberships = relationship("OrgMembership", back_populates="user", cascade="all, delete-orphan")
    oauth_connections = relationship("UserOAuthConnection", back_populates="user", cascade="all, delete-orphan")
