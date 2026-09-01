"""Mission — 任务空间主体（任务驱动的协作形态）。

设计见 docs/mission-space-p1-design.md §3.1/§3.7：
- 每个用户需求生成一个 Mission，编排器拆解为 MissionNode DAG 后自动派发执行
- 聊天降级为事件类型之一，全过程以 MissionEvent 事件流呈现并沉淀
- 双轨并存：现有协作空间零改动，Mission 挂现有 workspace_id（替代时无迁移）
- 状态/类型字段一律 String + 词表枚举（不用 PG Enum，仓库惯例）
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class MissionType(str, Enum):
    """任务形态：standard=标准 DAG；lightweight=轻任务（≤1 节点，跳过确认）。"""

    standard = "standard"
    lightweight = "lightweight"


class MissionStatus(str, Enum):
    """Mission 状态机（设计 §3.7）：draft → decomposing → awaiting_confirm → executing ⇄ blocked_question → acceptance → archived。"""

    draft = "draft"
    decomposing = "decomposing"
    awaiting_confirm = "awaiting_confirm"
    executing = "executing"
    blocked_question = "blocked_question"
    acceptance = "acceptance"
    archived = "archived"
    cancelled = "cancelled"


class Mission(BaseModel):
    """任务空间：需求原文 + 任务简报 + 状态与 token 成本汇总。"""

    __tablename__ = "missions"

    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 任务简报 {goal, constraints[], acceptance_criteria[], key_decisions[]}；
    # 编排器初生成，人工可编辑，每次编辑产生 brief_updated 事件
    brief: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    mission_type: Mapped[str] = mapped_column(
        String(16), default=MissionType.standard, nullable=False, server_default="standard"
    )
    status: Mapped[str] = mapped_column(
        String(24), default=MissionStatus.draft, nullable=False,
        server_default="draft", index=True,
    )
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # L0/L1/L2 规则覆盖，空=用 org 默认（MissionOrgConfig）
    escalation_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 轻任务产物隔离区天数，空=取 org 配置，org 空=默认 14
    artifact_ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 三项 token 成本汇总（节点完成时累加，口径同 WorkspaceTask，无 cached 维度）
    token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    prompt_token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    completion_token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # token 保险丝确认时间戳：人工确认后本次 Mission 豁免保险丝（防确认-再触发死循环，设计 §6）
    fuse_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 拆解来源记录 {engine, prompt_version, schema_version, decomposition_reason}
    coordinator_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
