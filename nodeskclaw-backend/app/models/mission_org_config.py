"""MissionOrgConfig — 任务空间 org 级配置（每 org 一行）。

设计见 docs/mission-space-p1-design.md §3.5：
- 不做启动预建；首次读取时 upsert（INSERT ... ON CONFLICT DO NOTHING 后 SELECT），
  更新走 API 时同样 upsert
- mission_token_fuse 为空=不启用保险丝；escalation_defaults 为空=平台内置默认
"""
from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class MissionOrgConfig(BaseModel):
    """org 级任务空间配置。"""

    __tablename__ = "mission_org_configs"
    __table_args__ = (
        # 每 org 一行（软删后允许重建，仓库 Partial Unique 惯例）
        Index(
            "uq_mission_org_configs_org",
            "org_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    # 轻任务产物隔离区天数（0-365，0 即不留）
    artifact_ttl_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default=text("14")
    )
    # 单 Mission 三项 token 成本总和阈值，超限挂起 + L2；空=不启用
    mission_token_fuse: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # L0/L1/L2 默认规则（危险操作清单等），空=平台内置默认
    escalation_defaults: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
