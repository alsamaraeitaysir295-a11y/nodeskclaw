"""任务空间工作流模板（设计见 docs/任务空间工作流模板与编排设计.md §二）。

验证通过的任务可保存为可复用模板——快照 Mission + MissionNode 的 DAG 结构，
复用时直接按模板建节点（跳过 LLM 拆解），结果可复现。
模式仿 instance_template.py 的主表+明细表。
"""
from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class MissionTemplate(BaseModel):
    """工作流模板主表：DAG 快照 + 元信息。"""

    __tablename__ = "mission_templates"

    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True,
    )
    # 空 = 组织级共享；非空 = 仅该空间可用
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 任务简报快照 {goal, constraints[], acceptance_criteria[], key_decisions[]}
    brief_template: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    # L2 规则快照
    escalation_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # standard / lightweight
    mission_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="standard", server_default="standard",
    )
    # auto / step_review（快照时的执行模式，复用时可覆盖）
    execution_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto", server_default="auto",
    )
    # 溯源 Mission
    created_from_mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True,
    )
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"),
    )
    # active / deprecated
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）


class MissionTemplateNode(BaseModel):
    """工作流模板节点明细：DAG 中单个节点的快照。"""

    __tablename__ = "mission_template_nodes"
    __table_args__ = (
        Index(
            "uq_mission_template_nodes_template_seq",
            "template_id", "seq",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mission_templates.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"),
    )
    # 依赖的上游节点 seq 列表
    depends_on: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"),
    )
