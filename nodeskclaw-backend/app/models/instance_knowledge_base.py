"""InstanceKnowledgeBase model - AI 员工与外挂知识库的直接关联表。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.knowledge_base import KnowledgeBase


class InstanceKnowledgeBase(BaseModel):
    __tablename__ = "instance_knowledge_bases"
    __table_args__ = (
        # 同一 instance 不能重复绑定同一知识库（软删除后可重新绑定）
        Index(
            "uq_instance_knowledge_bases_instance_kb",
            "instance_id",
            "kb_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instances.id"), nullable=False, index=True
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )

    # selectin：绑定量级小（单实例数个 KB），自动带出 KB 详情；
    # 此前用 noload 导致 attach 后 refresh 也拿不到 kb（恒 None），
    # POST /instances/{id}/knowledge-bases 响应序列化 500（2026-09-08 修复）
    kb: Mapped[KnowledgeBase] = relationship("KnowledgeBase", lazy="selectin")
