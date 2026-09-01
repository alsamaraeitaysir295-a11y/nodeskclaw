"""MissionNode — 任务空间内的执行节点（DAG 的一个子任务）。

设计见 docs/mission-space-p1-design.md §3.2：
- 编排器拆解输出，带能力标签（capability_tags，取自 CapabilityTag 词表）与依赖（depends_on，上游 seq）
- 状态机：pending → matched → dispatched → acked → running → done/failed/blocked_*/skipped
- session_key = {org_id}:{mission_id}:{node_id}，下发任务包时携带，实例内会话隔离
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class NodeStatus(str, Enum):
    """节点状态机（设计 §3.2）。"""

    pending = "pending"
    matched = "matched"
    dispatched = "dispatched"
    acked = "acked"
    running = "running"
    done = "done"
    failed = "failed"
    blocked_question = "blocked_question"
    blocked_dependency = "blocked_dependency"
    skipped = "skipped"


class MissionNode(BaseModel):
    """DAG 子任务：能力标签匹配实例后由调度器派发执行。"""

    __tablename__ = "mission_nodes"
    __table_args__ = (
        # 调度器每轮扫描"取 pending 且依赖全 done 的节点"（设计 §6）
        Index("ix_mission_nodes_mission_status", "mission_id", "status"),
        # 实例占用检查（NOT EXISTS dispatched/acked/running）的查询路径（设计 §6 实例串行保障）
        Index("ix_mission_nodes_instance_status", "assigned_instance_id", "status"),
        {
            "comment": "任务空间执行节点，按 capability_tags 匹配 AI 员工派发",
        },
    )

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 冗余 org_id（查询隔离用，不加 FK，设计明示）
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 节点创建序号（编排器输出顺序，depends_on 引用它）
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 能力标签（须来自 CapabilityTag 词表，编排器只允许从词表取值）
    capability_tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 依赖的上游节点 seq 列表（叶子节点为空数组）
    depends_on: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(24), default=NodeStatus.pending, nullable=False,
        server_default="pending", index=True,
    )
    assigned_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    # 匹配记录 {candidates:[{instance_id, score, basis}], chosen, at, capability_gap:[tags]}
    match_reason: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    session_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # 最近一次派发的幂等 ID（uuid4）
    last_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 三项 token 成本（口径同 WorkspaceTask，无 cached 维度）
    token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    prompt_token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    completion_token_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
