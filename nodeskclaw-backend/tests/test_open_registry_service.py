"""开放 Registry 服务层测试：public-only 口径、种子隐藏与豁免、脱敏序列化、分页钳制。"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from app.models.gene import Gene
from app.services.open_registry_service import (
    list_public_market_genes,
    list_public_featured,
    list_public_tags,
    get_public_market_gene_by_slug,
)
from tests.conftest import TestSessionLocal


async def _make_gene(**kw):
    """直建 Gene（uuid 后缀防同库冲突）；默认即对外可见的用户公共技能。"""
    suffix = uuid4().hex[:8]
    slug = kw.pop("slug", f"open-svc-{suffix}")
    defaults = dict(
        name=slug, slug=slug, visibility="public", review_status="approved",
        is_published=True, source="manual", created_by=None,
        manifest=json.dumps({"skill": {"content": "# t"}}, ensure_ascii=False),
        tags=json.dumps(kw.pop("tags", []), ensure_ascii=False),
        install_count=kw.pop("install_count", 0),
        is_featured=kw.pop("is_featured", False),
        lineage_group_id=str(uuid4()), version="1.0.0",
    )
    gene = Gene(**{**defaults, **kw})
    async with TestSessionLocal() as db:
        db.add(gene)
        await db.commit()
    return gene


async def test_list_visibility_filter():
    # install_count=10**6：保证 anchor gene 稳定落在 popular 排序 page 1（见下）
    visible = await _make_gene(install_count=10**6)
    org_private = await _make_gene(visibility="org_private")            # 组织私有
    pending = await _make_gene(review_status="pending_admin")           # 待审
    unpublished = await _make_gene(is_published=False)                  # 未发布
    personal = await _make_gene(visibility="personal")                  # 个人库
    soft_deleted = await _make_gene(
        deleted_at=datetime.now(timezone.utc))                          # 软删除
    async with TestSessionLocal() as db:
        items, _ = await list_public_market_genes(db, page_size=100)
    slugs = {i["slug"] for i in items}
    assert visible.slug in slugs
    assert org_private.slug not in slugs
    assert pending.slug not in slugs
    assert unpublished.slug not in slugs
    assert personal.slug not in slugs
    assert soft_deleted.slug not in slugs


async def test_seed_hidden_but_exempt_slug_visible(monkeypatch):
    """官方种子默认隐藏，但豁免集合内的 slug 可见（机制测试，不绑死 market-client）。"""
    from app.services import open_registry_service as svc

    exempt_slug = f"exempt-{uuid4().hex[:8]}"
    monkeypatch.setattr(svc, "EXPOSED_SEED_SLUGS", {exempt_slug})
    hidden_prefix = f"seed-hidden-{uuid4().hex[:6]}"
    await _make_gene(source="official", slug=hidden_prefix)
    # 高 install_count：共享测试库已积累 100+ 零安装量可见 gene（drop_all 失效），
    # popular 排序无次级键，零安装量的新建 gene 可能落在 page_size=100 之外
    await _make_gene(source="official", slug=exempt_slug, install_count=10**6)
    async with TestSessionLocal() as db:
        items, _ = await svc.list_public_market_genes(db, page_size=100)
    slugs = {i["slug"] for i in items}
    assert exempt_slug in slugs
    assert not any(s.startswith("seed-hidden-") for s in slugs)


async def test_get_by_slug_uses_visibility_filter():
    hidden = await _make_gene(visibility="org_private")
    visible = await _make_gene()
    async with TestSessionLocal() as db:
        assert await get_public_market_gene_by_slug(db, hidden.slug) is None
        got = await get_public_market_gene_by_slug(db, visible.slug)
    assert got is not None and got.slug == visible.slug


async def test_sanitized_fields():
    gene = await _make_gene(dependencies=json.dumps(["dep-a"]), synergies=json.dumps(["syn-b"]))
    async with TestSessionLocal() as db:
        items, _ = await list_public_market_genes(db, keyword=gene.slug)
    assert items, "keyword 应命中刚建的 gene"
    item = items[0]
    allowed = {"slug", "name", "description", "short_description", "category", "tags",
               "version", "icon", "install_count", "avg_rating", "effectiveness_score",
               "is_featured", "dependencies", "synergies", "created_at", "updated_at"}
    assert set(item.keys()) == allowed
    assert item["dependencies"] == ["dep-a"]


async def test_tags_and_featured():
    await _make_gene(tags=["alpha", "beta"], is_featured=True, install_count=9)
    await _make_gene(tags=["alpha"])
    async with TestSessionLocal() as db:
        tags = await list_public_tags(db)
        featured = await list_public_featured(db, limit=10)
    tag_map = {t["tag"]: t["count"] for t in tags}
    assert tag_map.get("alpha", 0) >= 2
    assert all(f["is_featured"] for f in featured)


async def test_page_size_clamped_no_crash():
    await _make_gene()
    async with TestSessionLocal() as db:
        items, total = await list_public_market_genes(db, page_size=10000)
    assert total >= 1 and len(items) >= 1
