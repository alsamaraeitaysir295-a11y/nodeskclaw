"""开放 Registry API 测试：匿名可达、口径过滤、脱敏、ZIP 下载、限流 429、开关 404、路由顺序。"""

import json
import zipfile
import io
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core import open_registry_rate_limit as rl
from app.core.config import settings
from app.core.security import get_current_user
from app.main import app
from app.models.gene import Gene
from tests.conftest import TestSessionLocal


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rl.reset_open_registry_rate_limits()
    yield
    rl.reset_open_registry_rate_limits()


async def _make_gene(**kw):
    suffix = uuid4().hex[:8]
    slug = kw.pop("slug", f"open-api-{suffix}")
    defaults = dict(
        name=slug, slug=slug, visibility="public", review_status="approved",
        is_published=True, source="manual", created_by=None,
        manifest=json.dumps(
            kw.pop("manifest", {"skill": {"content": "# hello"},
                                "scripts": {"main.py": "print(1)"}}),
            ensure_ascii=False),
        tags=json.dumps(kw.pop("tags", []), ensure_ascii=False),
        install_count=kw.pop("install_count", 0),
        lineage_group_id=str(uuid4()), version="1.0.0",
    )
    gene = Gene(**{**defaults, **kw})
    async with TestSessionLocal() as db:
        db.add(gene)
        await db.commit()
    return gene


async def test_list_anonymous_and_visibility(client):
    # 高 install_count：共享测试库已积累 100+ 零安装量可见 gene（drop_all 失效），
    # popular 排序无次级键，零安装量的新建 gene 可能落在 page_size=100 之外
    visible = await _make_gene(install_count=10**6)
    await _make_gene(visibility="org_private")
    await _make_gene(review_status="pending_admin")
    await _make_gene(is_published=False)
    r = await client.get("/registry/api/v1/genes", params={"page_size": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    slugs = {i["slug"] for i in body["data"]["items"]}
    assert visible.slug in slugs


async def _purge_gene(slug: str):
    """测试库按 slug 硬清（conftest 的 drop_all 因 FK 环实际从未生效，
    固定 slug 的 market-client 跨会话会累积触发 name 唯一索引）。"""
    async with TestSessionLocal() as db:
        await db.execute(delete(Gene).where(Gene.slug == slug))
        await db.commit()


async def test_list_hides_official_seeds_except_market_client(client):
    await _purge_gene("market-client")
    await _make_gene(source="official", slug=f"seed-x-{uuid4().hex[:6]}")
    # install_count 同上：保证 market-client 稳定落在 popular 排序 page 1
    await _make_gene(source="official", slug="market-client",
                     manifest={"skill": {"content": "# bootstrap"}},
                     install_count=10**6)
    r = await client.get("/registry/api/v1/genes", params={"page_size": 100})
    slugs = {i["slug"] for i in r.json()["data"]["items"]}
    assert "market-client" in slugs
    assert not any(s.startswith("seed-x-") for s in slugs)


async def test_detail_sanitized(client):
    gene = await _make_gene()
    r = await client.get(f"/registry/api/v1/genes/{gene.slug}")
    assert r.status_code == 200
    raw = r.text
    for forbidden in ("created_by", "org_id", "lineage_group_id",
                      "parent_gene_id", "review_status", "manifest"):
        assert forbidden not in raw
    item = r.json()["data"]
    assert item["slug"] == gene.slug


async def test_detail_404_for_hidden(client):
    for kw in ({"visibility": "org_private"},
               {"review_status": "pending_admin"},
               {"is_published": False}):
        g = await _make_gene(**kw)
        r = await client.get(f"/registry/api/v1/genes/{g.slug}")
        assert r.status_code == 404, kw


async def test_manifest_returns_skill_body(client):
    gene = await _make_gene(manifest={
        "skill": {"content": "# full body"},
        "scripts": {"run.py": "print('x')"},
    })
    r = await client.get(f"/registry/api/v1/genes/{gene.slug}/manifest")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["skill"]["content"] == "# full body"
    assert data["scripts"]["run.py"] == "print('x')"


async def test_download_zip(client):
    gene = await _make_gene()
    r = await client.get(f"/registry/api/v1/genes/{gene.slug}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert f"{gene.slug}/SKILL.md" in names
    assert f"{gene.slug}/main.py" in names


async def test_download_non_dict_manifest_returns_400(client):
    # Task 1 遗留加固：合法 JSON 但非 dict（如 "[1,2]"）时 build_gene_zip
    # 内 manifest.get 抛 AttributeError，需归一为 400 而非 500
    gene = await _make_gene(manifest=[1, 2])
    r = await client.get(f"/registry/api/v1/genes/{gene.slug}/download")
    assert r.status_code == 400
    assert r.json()["message_key"] == "errors.gene.manifest_corrupt"


async def test_tags_and_featured_not_swallowed_by_slug_route(client):
    await _make_gene(tags=["zt-alpha"], is_featured=True)
    r1 = await client.get("/registry/api/v1/genes/tags")
    r2 = await client.get("/registry/api/v1/genes/featured")
    assert r1.status_code == 200 and any(
        t["tag"] == "zt-alpha" for t in r1.json()["data"])
    assert r2.status_code == 200


async def test_page_size_over_100_accepted(client):
    await _make_gene()
    r = await client.get("/registry/api/v1/genes", params={"page_size": 10000})
    assert r.status_code == 200
    assert len(r.json()["data"]["items"]) <= 100


async def test_rate_limit_429(client, monkeypatch):
    monkeypatch.setattr(
        "app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 3)
    for _ in range(3):
        assert (await client.get("/registry/api/v1/genes")).status_code == 200
    r = await client.get("/registry/api/v1/genes")
    assert r.status_code == 429
    assert r.json()["code"] == 42900
    assert r.json()["message_key"] == "errors.common.too_many_attempts"


async def test_registry_no_store_cache_control(client):
    """app 级 _NoCacheAPIMiddleware 需同时覆盖 /registry/（no-store 语义）。"""
    r = await client.get("/registry/api/v1/genes")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


class _FakePortalUser:
    """最小可用登录用户：门户 download 只用到 id / current_org_id / is_super_admin。"""

    id = "u-open-portal-dl"
    current_org_id = "org-open-portal-dl"
    is_super_admin = False


async def test_portal_download_gene_zip(client):
    """门户侧 GET /api/v1/genes/{slug}/download：登录态 + public gene → 200，zip 内含
    {slug}/SKILL.md（与 /registry download 端点复用同一打包服务，此处直测门户入口）。"""
    gene = await _make_gene()
    app.dependency_overrides[get_current_user] = lambda: _FakePortalUser()
    try:
        r = await client.get(f"/api/v1/genes/{gene.slug}/download")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert f"{gene.slug}/SKILL.md" in names
    assert f"{gene.slug}/main.py" in names


async def test_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_ENABLED", False)
    for path in ("/registry/api/v1/genes",
                 "/registry/api/v1/genes/tags",
                 "/registry/api/v1/genes/featured"):
        assert (await client.get(path)).status_code == 404, path


async def test_llms_txt(client):
    r = await client.get("/registry/llms.txt")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    body = r.text
    for needle in ("/registry/api/v1/genes",
                   "/registry/api/v1/genes/{slug}/manifest",
                   "/registry/api/v1/genes/{slug}/download",
                   "/registry/llms.txt",
                   "market-client"):
        assert needle in body, needle
    # 零漂移：文档中的限流数字必须与 settings 实际值一致（F3 动态化）
    assert str(settings.OPEN_REGISTRY_READ_RATE_LIMIT) in body
    assert str(settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT) in body


async def test_market_client_template_valid():
    import pathlib

    import app.main as main_module

    tpl_path = (
        pathlib.Path(main_module.__file__).parent
        / "data" / "gene_templates" / "market_client.json"
    )
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    assert tpl["slug"] == "market-client"
    content = tpl["manifest"]["skill"]["content"]
    assert content.startswith("---")                       # SKILL.md frontmatter
    assert "/registry/llms.txt" in content                 # 教 agent 拿自描述
    assert "/registry/api/v1/genes" in content             # 教 agent 调端点
