"""Organization (tenant) model."""

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Organization(BaseModel):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)

    # 套餐与配额
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    max_instances: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_cpu_total: Mapped[str] = mapped_column(String(16), default="4", nullable=False)
    max_mem_total: Mapped[str] = mapped_column(String(16), default="8Gi", nullable=False)
    max_storage_total: Mapped[str] = mapped_column(String(16), default="500Gi", nullable=False)
    max_collaboration_depth: Mapped[int] = mapped_column(Integer, default=3, nullable=False, server_default="3")

    # 专属集群（null = 共享集群）
    cluster_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 外部智能体插件的 SSRF 白名单（CIDR 列表，如 ["10.0.0.0/8", "172.16.0.0/12"]）。
    # 默认空列表 → 拦截所有私网 / 回环地址；只有 org admin 在该字段填入私网 CIDR 后，
    # 才能提交 endpoint 在该 CIDR 内的外部插件。
    # 云元数据地址 169.254.169.254 / 0.0.0.0 / :: 是硬禁，不会被此字段放行。
    external_agent_allowed_cidrs: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # relationships
    dedicated_cluster = relationship("Cluster", foreign_keys=[cluster_id])
    memberships = relationship("OrgMembership", back_populates="organization", cascade="all, delete-orphan")
    instances = relationship("Instance", back_populates="organization")
    oauth_bindings = relationship("OrgOAuthBinding", back_populates="organization", cascade="all, delete-orphan")
