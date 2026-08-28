"""技能市场统计服务（下载 / 使用事件的记录与榜单聚合）。

数据流：
- 写入端 A（后端埋点）：fork 端点、zip 下载端点在成功后调 record_event；
- 写入端 B（llm-proxy）：解析模型 tool_calls 归因到 gene 后直连同库 INSERT
  skill_use 事件（见 nodeskclaw-llm-proxy/app/skill_usage.py，不走本函数，
  但表结构以本模块的 GeneMarketEvent 为准）；
- 读取端：GET /genes/market-stats 按维度（总/月/周）聚合两榜。

口径（与产品确认，见 docs/技能市场统计功能方案.md）：
- 下载榜 = zip_download + install（员工配置界面安装）+ fork(target_scope='personal')；
  fork 到 org/public 也记录但不进下载榜（留未来口径）。
- 使用榜 = skill_use（模型实际发起、可归因到 skill 的调用）。
- 数据范围 = 公共市场（visibility='public'）+ 当前组织的基因。
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import Integer, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.gene import Gene
from app.models.gene_market_event import GeneMarketEvent

logger = logging.getLogger(__name__)

# 每个榜单返回的条数
RANKING_LIMIT = 10

# 平台种子基因（source=official 且无创建者，含 nodeskclaw-* 协作工具与预置技能）：
# 不属于用户在技能市场获取/使用的内容，不进榜单（产品口径 2026-08-27，
# 与 list_genes / local_adapter 的列表隐藏规则保持一致，同步修改）
SEED_GENE_CONDITION = and_(Gene.source == "official", Gene.created_by.is_(None))

# 合法维度值（router 侧也做校验，这里双重保险）
DIMENSIONS = ("total", "month", "week")


def _cutoff_for(dimension: str) -> datetime | None:
    """按维度返回统计时间窗起点（本地时区，与部署 TZ=Asia/Shanghai 一致）。

    month → 本月 1 号 0 点；week → 本周一 0 点（ISO 周，周一为一周开始）；
    total → None（不限）。
    """
    now = datetime.now()
    if dimension == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dimension == "week":
        # weekday(): 周一=0；回退到本周一 0 点
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


async def record_event(
    *,
    gene: Gene,
    event_type: str,
    db: AsyncSession,
    user_id: str | None = None,
    org_id: str | None = None,
    target_scope: str | None = None,
) -> None:
    """埋点写入（fork / zip_download）：失败仅 warning，不影响主流程。"""
    try:
        db.add(GeneMarketEvent(
            gene_id=gene.id,
            gene_slug=gene.slug,
            gene_name=gene.name,
            event_type=event_type,
            target_scope=target_scope,
            user_id=user_id,
            org_id=org_id,
        ))
        await db.commit()
    except Exception:
        logger.warning("gene market event 写入失败 gene=%s type=%s", gene.slug, event_type, exc_info=True)
        await db.rollback()


async def _ranking(
    db: AsyncSession,
    *,
    org_id: str,
    cutoff: datetime | None,
    event_filter,
) -> list[dict]:
    """按事件过滤条件聚合 Top N 榜单。

    event_filter 为作用于 GeneMarketEvent 的 SQLAlchemy 表达式。
    同一技能多 scope 并存（fork 三向），榜单按 slug 聚合合并各副本计数；
    范围 = 公共 + 本组织 + personal（安装/使用可发生在任意 scope 副本上）。
    """
    conditions = [
        not_deleted(GeneMarketEvent),
        not_deleted(Gene),
        event_filter,
        or_(
            Gene.visibility == "public",
            Gene.org_id == org_id,
            Gene.visibility == "personal",
        ),
        # 平台种子基因不进榜单
        not_(SEED_GENE_CONDITION),
    ]
    if cutoff is not None:
        conditions.append(GeneMarketEvent.created_at >= cutoff)

    rows = (await db.execute(
        select(
            GeneMarketEvent.gene_slug,
            func.max(GeneMarketEvent.gene_name).label("name"),
            func.count().cast(Integer).label("cnt"),
        )
        .join(Gene, Gene.id == GeneMarketEvent.gene_id)
        .where(and_(*conditions))
        .group_by(GeneMarketEvent.gene_slug)
        .order_by(func.count().desc())
        .limit(RANKING_LIMIT)
    )).all()
    return [
        {"slug": r.gene_slug, "name": r.name, "count": r.cnt}
        for r in rows
    ]


async def get_market_stats(
    db: AsyncSession,
    *,
    org_id: str,
    dimension: str = "total",
) -> dict:
    """技能市场两榜聚合：下载榜（zip + 个人 fork）与使用榜（skill_use）。

    totals 为对应口径的事件总条数（不限于榜单内基因），供汇总卡展示。
    """
    if dimension not in DIMENSIONS:
        dimension = "total"
    cutoff = _cutoff_for(dimension)

    # 下载口径：zip 下载 + fork 到个人库 + 员工配置界面安装（三类获取动作同榜）
    download_filter = or_(
        GeneMarketEvent.event_type == "zip_download",
        GeneMarketEvent.event_type == "install",
        and_(
            GeneMarketEvent.event_type == "fork",
            GeneMarketEvent.target_scope == "personal",
        ),
    )
    use_filter = GeneMarketEvent.event_type == "skill_use"

    download_ranking = await _ranking(
        db, org_id=org_id, cutoff=cutoff, event_filter=download_filter,
    )
    use_ranking = await _ranking(
        db, org_id=org_id, cutoff=cutoff, event_filter=use_filter,
    )

    async def _total(event_filter) -> int:
        conditions = [
            not_deleted(GeneMarketEvent),
            not_deleted(Gene),
            event_filter,
            or_(
                Gene.visibility == "public",
                Gene.org_id == org_id,
                Gene.visibility == "personal",
            ),
            not_(SEED_GENE_CONDITION),
        ]
        if cutoff is not None:
            conditions.append(GeneMarketEvent.created_at >= cutoff)
        return (await db.execute(
            select(func.count())
            .select_from(GeneMarketEvent)
            .join(Gene, Gene.id == GeneMarketEvent.gene_id)
            .where(and_(*conditions))
        )).scalar() or 0

    return {
        "dimension": dimension,
        "totals": {
            "download": await _total(download_filter),
            "use": await _total(use_filter),
        },
        "rankings": {
            "download": download_ranking,
            "use": use_ranking,
        },
    }
