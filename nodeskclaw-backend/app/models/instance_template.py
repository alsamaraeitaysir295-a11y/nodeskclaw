"""AI Employee Template model."""

from enum import Enum

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel
from app.models.gene import ContentVisibility


class TemplateItemType(str, Enum):
    gene = "gene"
    genome = "genome"


class InstanceTemplate(BaseModel):
    __tablename__ = "instance_templates"
    __table_args__ = (
        Index(
            "uq_instance_templates_slug_org_active",
            "slug",
            "org_id",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gene_slugs: Mapped[str | None] = mapped_column(Text, nullable=True)  # deprecated, use template_items

    source_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("instances.id"), nullable=True
    )

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(16), default=ContentVisibility.public, nullable=False,
        server_default="public",
    )
    # 待审 / 已批准 / 已拒绝；None 视同 approved（存量数据兼容，参照 Gene 的既有约定）
    review_status: Mapped[str | None] = mapped_column(String(16), nullable=True)


class TemplateItem(BaseModel):
    __tablename__ = "template_items"
    __table_args__ = (
        Index(
            "uq_template_item_active",
            "template_id",
            "item_type",
            "item_slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instance_templates.id"),
        nullable=False, index=True,
    )
    item_type: Mapped[str] = mapped_column(String(16), nullable=False)
    item_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
