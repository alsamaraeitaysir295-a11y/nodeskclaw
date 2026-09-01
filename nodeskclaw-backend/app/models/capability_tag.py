"""CapabilityTag — 能力词表（编排器拆解与匹配器的公共标签字典）。

设计见 docs/mission-space-p1-design.md §3.6：
- org_id 为空 = 平台预置词表（迁移 seed 约 20 个）；org 管理员可扩展自己的词表
- 编排器拆解时只允许从词表取值；匹配器用标签与实例能力串做确定性比较
- 注意 NULL 语义：PG 唯一索引里 NULL 不相等，平台词表（org_id=NULL）的
  重复插入不被索引拦截——seed 幂等靠应用侧 exists 检查（org_id IS NULL AND tag=?）
"""
from enum import Enum

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class TagStatus(str, Enum):
    """词表条目状态：deprecated 的标签不再供编排器选用。"""

    active = "active"
    deprecated = "deprecated"


class CapabilityTag(BaseModel):
    """能力标签（英文 kebab-case，如 backend / data-analysis）。"""

    __tablename__ = "capability_tags"
    __table_args__ = (
        # 同一 scope 内 tag 唯一（软删后可复用，仓库 Partial Unique 惯例）
        Index(
            "uq_capability_tags_org_tag",
            "org_id", "tag",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    # 空 = 平台预置词表
    org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label_zh: Mapped[str | None] = mapped_column(String(100), nullable=True)
    label_en: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 语义说明，供编排器 prompt 使用
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=TagStatus.active, nullable=False, server_default="active"
    )
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
