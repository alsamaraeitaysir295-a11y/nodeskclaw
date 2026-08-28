"""技能市场分类模型（管理员可编辑的分类字典）。

设计要点：
- 分类列表平台级共享（不按 org 隔离）：CE 单组织部署下全局一份即可
- 首次访问时由 service 播种默认八类（开发/数据/运维/网络/创意/沟通/安全/效率）
- name 唯一（Partial Unique Index，软删除后可复用同名，符合仓库约定）
- 管理入口：PUT /admin/genes/categories 全量替换（org admin 权限）
"""
from sqlalchemy import Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class GeneCategory(BaseModel):
    """技能市场分类（字典表，按 sort_order 排序展示）。"""

    __tablename__ = "gene_categories"
    __table_args__ = (
        # 软删除后允许同名重建：唯一约束仅约束未删除行（Partial Unique Index，仓库约定）
        Index(
            "uq_gene_categories_name_active",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": "技能市场分类字典，管理员可编辑",
        },
    )

    name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0,
    )
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
