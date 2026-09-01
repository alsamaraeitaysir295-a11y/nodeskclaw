"""MissionEvent — 任务空间统一事件流 + seq 计数行。

设计见 docs/mission-space-p1-design.md §3.3/§3.8：
- 单一写入路径 MissionEventService.append（T2），seq 靠 mission_event_counters
  计数行 UPDATE ... RETURNING 递增（同一 Mission 事件写入天然串行化；
  禁用 SELECT max()+1——聚合加 FOR UPDATE 是无效锁）
- visibility 只区分 timeline（仅前端展示，不注入上下文）/ both；心跳与工具播报=timeline
- MissionEventCounter 直继 Base（非 BaseModel）：基础设施无软删语义，
  计数行被软删会破坏 seq 生成
"""
from enum import Enum

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseModel


class MissionEventType(str, Enum):
    """事件类型词表（设计 §3.8）。"""

    mission_created = "mission_created"
    decomposition_started = "decomposition_started"
    decomposition_done = "decomposition_done"
    decomposition_failed = "decomposition_failed"
    dag_confirmed = "dag_confirmed"
    dag_edited = "dag_edited"
    brief_updated = "brief_updated"
    dispatched = "dispatched"
    task_ack = "task_ack"
    progress = "progress"                # timeline
    tool_trace = "tool_trace"            # timeline
    narrative = "narrative"              # timeline
    heartbeat = "heartbeat"              # timeline
    l1_question = "l1_question"
    l2_question = "l2_question"
    question_answered = "question_answered"
    blocked = "blocked"
    unblocked = "unblocked"
    node_done = "node_done"
    node_failed = "node_failed"
    artifact_produced = "artifact_produced"
    artifact_promoted = "artifact_promoted"
    artifact_expired = "artifact_expired"
    mission_accepted = "mission_accepted"
    mission_rejected = "mission_rejected"
    mission_cancelled = "mission_cancelled"
    token_fuse_tripped = "token_fuse_tripped"
    system_note = "system_note"


class ActorType(str, Enum):
    """事件发起者类型。"""

    user = "user"
    agent = "agent"
    scheduler = "scheduler"
    system = "system"


class EventVisibility(str, Enum):
    """事件可见性：timeline=仅前端展示不注入上下文；both=两者。"""

    timeline = "timeline"
    both = "both"


class MissionEvent(BaseModel):
    """任务空间的单条事件（播报/提问/状态变迁/产物元数据）。"""

    __tablename__ = "mission_events"
    __table_args__ = (
        # Mission 内 seq 单调递增；软删行不参与唯一（仓库 Partial Unique 惯例）。
        # 唯一索引同时覆盖 ?after_seq= 增量拉取的查询路径
        Index(
            "uq_mission_events_mission_seq",
            "mission_id", "seq",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 系统级事件为空（节点独立软删，事件不级联）
    node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mission_nodes.id"), nullable=True, index=True
    )
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 冗余显示名（前端三级回退用，避免裸显 UUID）
    actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), default=EventVisibility.both, nullable=False, server_default="both"
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）


class MissionEventCounter(Base):
    """mission_event_counters：per-mission seq 计数行（基础设施表，无软删/时间戳）。

    写事件时 UPDATE mission_event_counters SET next_seq = next_seq + 1
    WHERE mission_id = ? RETURNING next_seq——行级更新天然串行化同一
    Mission 的事件写入（设计 §3.3 定案，禁用 max()+1）。Mission 创建时同步插入。
    """

    __tablename__ = "mission_event_counters"

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id", ondelete="CASCADE"), primary_key=True
    )
    next_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
