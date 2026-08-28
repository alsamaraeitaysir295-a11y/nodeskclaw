"""技能市场事件模型（下载 / 使用埋点）。

技能市场统计页的数据源。设计要点：
- 每次可统计事件落一条：fork（获取到库）、zip_download（下载到本地）、
  install（员工配置界面安装）、skill_use（llm-proxy 解析到的模型实际调用，
  见 nodeskclaw-llm-proxy/app/skill_usage.py）。
- gene_slug / gene_name 反范式冗余：gene 被删除后榜单仍可展示名称。
- 写入方有后端埋点（fork/zip/install）与 llm-proxy（skill_use，直连同库 INSERT），
  两端都要求非阻塞写入（失败仅日志，不影响主流程）。
- 下载榜口径 = fork(target_scope='personal') + zip_download + install 求和；
  fork 到 org/public 也记录（target_scope 区分），留作未来口径扩展。
- 软删除继承 BaseModel.deleted_at（查询一律过滤）；gene 删除不级联本表
  （保留历史统计）。
"""
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class GeneMarketEvent(BaseModel):
    """技能市场的单次下载 / 使用事件。"""

    __tablename__ = "gene_market_events"
    # 榜单按 (gene_id, event_type, created_at) / (event_type, org_id, created_at) 聚合
    __table_args__ = (
        Index(
            "ix_gene_market_events_gene_type_created",
            "gene_id", "event_type", "created_at",
        ),
        Index(
            "ix_gene_market_events_type_org_created",
            "event_type", "org_id", "created_at",
        ),
    )

    gene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("genes.id"),
        nullable=False,
        index=True,
    )
    gene_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    gene_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'fork' | 'zip_download' | 'install' | 'skill_use'
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # fork 目标：'personal' | 'org' | 'public'（其他事件为空）
    target_scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
