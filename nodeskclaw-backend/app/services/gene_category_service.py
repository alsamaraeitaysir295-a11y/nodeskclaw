"""技能市场分类字典服务。

分类列表平台级共享、管理员可编辑（PUT /admin/genes/categories 全量替换）。
首次访问自动播种默认八类；替换采用 diff（保留未变行的 id/时间，软删除移除项，
新增项追加），避免整表重建导致的高频 id 抖动。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.gene_category import GeneCategory

logger = logging.getLogger(__name__)

# 默认分类（与前端旧硬编码列表一致，首次播种用）
DEFAULT_CATEGORIES = ["开发", "数据", "运维", "网络", "创意", "沟通", "安全", "效率"]


async def ensure_seeded(db: AsyncSession) -> None:
    """表为空时播种默认分类（幂等）。"""
    count = (await db.execute(
        select(GeneCategory).where(not_deleted(GeneCategory)).limit(1)
    )).scalars().first()
    if count is not None:
        return
    for i, name in enumerate(DEFAULT_CATEGORIES):
        db.add(GeneCategory(name=name, sort_order=i))
    await db.commit()


async def list_categories(db: AsyncSession) -> list[dict]:
    """返回启用中的分类（按 sort_order → name）。"""
    await ensure_seeded(db)
    rows = (await db.execute(
        select(GeneCategory)
        .where(not_deleted(GeneCategory))
        .order_by(GeneCategory.sort_order, GeneCategory.name)
    )).scalars().all()
    return [{"id": r.id, "name": r.name, "sort_order": r.sort_order} for r in rows]


async def replace_categories(db: AsyncSession, names: list[str]) -> list[dict]:
    """管理员全量替换分类列表。

    - 顺序即传入顺序（sort_order 重排为下标）
    - 已存在且保留的行只更新 sort_order；新增的插入；缺失的软删除
    - 名称去重 + 去空白；空列表允许（清空所有分类）
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if name and name not in seen and len(name) <= 32:
            cleaned.append(name)
            seen.add(name)

    existing = (await db.execute(
        select(GeneCategory).where(not_deleted(GeneCategory))
    )).scalars().all()
    by_name = {r.name: r for r in existing}

    keep_ids: set[str] = set()
    for i, name in enumerate(cleaned):
        row = by_name.get(name)
        if row is None:
            row = GeneCategory(name=name, sort_order=i)
            db.add(row)
        else:
            row.sort_order = i
            keep_ids.add(row.id)

    for row in existing:
        if row.id not in keep_ids:
            # 软删除：打标记而非物理 DELETE（仓库约定）
            row.deleted_at = datetime.now(timezone.utc)
            db.add(row)

    await db.commit()
    return await list_categories(db)
