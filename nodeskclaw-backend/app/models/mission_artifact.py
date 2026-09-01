"""MissionArtifact — 任务空间产物（节点执行产出）。

设计见 docs/mission-space-p1-design.md §3.4：
- 产物先进 TTL 隔离区（quarantine），用户点"保留"转正式（promoted，expires_at 置空）
- 清理协程每小时扫 retention='quarantine' AND expires_at < now()，物理删除存储对象
  （对"删除一律软删除"规则的有意例外：隔离区 TTL 是履约清理，行级软删 + 事件留痕均在）
- 同名产物再次提交 version+1，prev_artifact_id 记在 payload
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ArtifactKind(str, Enum):
    """产物类型。"""

    file = "file"
    report = "report"
    code = "code"
    config = "config"
    other = "other"


class ArtifactRetention(str, Enum):
    """产物留存状态：quarantine=TTL 隔离区；promoted=用户保留转正。"""

    quarantine = "quarantine"
    promoted = "promoted"


class MissionArtifact(BaseModel):
    """节点执行产物（文件/报告/代码等），存储走 storage_service。"""

    __tablename__ = "mission_artifacts"
    __table_args__ = (
        # 每小时清理协程的扫描路径（retention + expires_at 过滤）
        Index("ix_mission_artifacts_retention_expires", "retention", "expires_at"),
    )

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mission_nodes.id"), nullable=True, index=True
    )
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 产物名（含扩展名）
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 存储路径 missions/{org_id}/{workspace_id}/{mission_id}/{artifact_id}/{name}
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 同名产物再次提交 version+1（lineage 的 prev_artifact_id 记在 payload）
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    retention: Mapped[str] = mapped_column(
        String(16), default=ArtifactRetention.quarantine, nullable=False,
        server_default="quarantine",
    )
    # quarantine 必填 = now + mission TTL；promoted 置空
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    produced_by_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    promoted_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
