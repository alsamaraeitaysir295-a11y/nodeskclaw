"""开放 Registry 服务层：对外（匿名）技能市场的 public-only 查询与脱敏序列化。

口径与门户市场一致（参照 gene_service._list_genes_local）：
- 仅 visibility=public 且 (approved | 历史无审核态 NULL) 且 is_published 且未删除
- 列表隐藏平台种子基因（source=official 且 created_by 为空），唯一豁免 market-client
  （外部 agent 的引导技能，必须可被列表发现）
- 对外序列化仅市场元数据，绝不包含 created_by / org_id / lineage_group_id 等
  内部标识与 manifest 本体（列表/详情不含，取本体走 manifest/download 端点）
"""

from __future__ import annotations

import json

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.gene import Gene, GeneReviewStatus

# 种子豁免：引导技能必须在开放列表可见
EXPOSED_SEED_SLUGS = {"market-client"}

_PAGE_SIZE_MAX = 100


def _json_loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _public_visible_filter():
    return and_(
        Gene.visibility == "public",
        or_(
            Gene.review_status == GeneReviewStatus.approved,
            Gene.review_status.is_(None),
        ),
        Gene.is_published.is_(True),
        not_deleted(Gene),
    )


def _list_visible_filter():
    """列表口径 = 可见过滤 + 种子隐藏（豁免 EXPOSED_SEED_SLUGS）。

    list_public_market_genes 与 list_public_tags 必须同源使用本谓词，
    否则 tags 宣称的标签在列表里搜不到（两端口径矛盾）。
    """
    return and_(
        _public_visible_filter(),
        not_(
            and_(
                Gene.source == "official",
                Gene.created_by.is_(None),
                Gene.slug.notin_(EXPOSED_SEED_SLUGS),
            )
        ),
    )


def public_gene_to_dict(gene: Gene) -> dict:
    """对外脱敏序列化：仅市场元数据。"""
    return {
        "slug": gene.slug,
        "name": gene.name,
        "description": gene.description,
        "short_description": gene.short_description,
        "category": gene.category,
        "tags": _json_loads(gene.tags) or [],
        "version": gene.version,
        "icon": gene.icon,
        "install_count": gene.install_count,
        "avg_rating": gene.avg_rating,
        "effectiveness_score": gene.effectiveness_score,
        "is_featured": gene.is_featured,
        "dependencies": _json_loads(gene.dependencies) or [],
        "synergies": _json_loads(gene.synergies) or [],
        "created_at": gene.created_at,
        "updated_at": gene.updated_at,
    }


async def list_public_market_genes(
    db: AsyncSession,
    *,
    keyword: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    sort: str = "popular",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), _PAGE_SIZE_MAX)

    base = select(Gene).where(_list_visible_filter())
    if keyword:
        base = base.where(
            Gene.name.ilike(f"%{keyword}%") | Gene.slug.ilike(f"%{keyword}%")
        )
    if tag:
        base = base.where(Gene.tags.ilike(f'%"{tag}"%'))
    if category:
        base = base.where(Gene.category == category)

    count_q = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    sort_map = {
        "popular": Gene.install_count.desc(),
        "rating": Gene.avg_rating.desc(),
        "newest": Gene.created_at.desc(),
    }
    base = (
        base.order_by(sort_map.get(sort, Gene.install_count.desc()))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    genes = (await db.execute(base)).scalars().all()
    return [public_gene_to_dict(g) for g in genes], total


async def get_public_market_gene_by_slug(db: AsyncSession, slug: str) -> Gene | None:
    """按 slug 直取对外可见 gene（.scalars().first()——同 slug 多行坑）。"""
    result = await db.execute(
        select(Gene).where(_public_visible_filter(), Gene.slug == slug)
    )
    return result.scalars().first()


async def list_public_tags(db: AsyncSession) -> list[dict]:
    """聚合列表口径下 gene 的标签（与列表同源过滤）。市场为百级规模，Python 侧聚合足够。"""
    result = await db.execute(
        select(Gene.tags).where(_list_visible_filter(), Gene.tags.isnot(None))
    )
    counts: dict[str, int] = {}
    for (raw,) in result.all():
        for t in (_json_loads(raw) or []):
            if isinstance(t, str) and t:
                counts[t] = counts.get(t, 0) + 1
    return [
        {"tag": k, "count": v}
        for k, v in sorted(counts.items(), key=lambda x: -x[1])
    ]


async def list_public_featured(db: AsyncSession, *, limit: int = 10) -> list[dict]:
    limit = min(max(limit, 1), _PAGE_SIZE_MAX)
    result = await db.execute(
        select(Gene)
        .where(_public_visible_filter(), Gene.is_featured.is_(True))
        .order_by(Gene.install_count.desc())
        .limit(limit)
    )
    return [public_gene_to_dict(g) for g in result.scalars().all()]
