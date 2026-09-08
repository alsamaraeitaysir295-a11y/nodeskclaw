# 开放技能市场 Registry 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 平台作为开放数据提供方，匿名只读对外暴露公共技能市场（GeneHub 协议形状的 `/registry` API + llms.txt 自描述 + market-client 引导技能）。

**Architecture:** 新增免登录 router 挂 `/registry` 前缀（与门户 `/api/v1` 隔离），服务层独立新文件做 public-only 查询与脱敏序列化；ZIP 打包逻辑从门户 download 端点抽取为共享 helper 复用；IP 滑动窗口限流 + settings 开关。设计依据：`ee/docs/开放技能市场Registry设计.md`。

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy(async) + pytest(asyncio auto) + httpx ASGITransport

## Global Constraints

- 所有命令在 `nodeskclaw-backend/` 目录下执行：`uv run pytest <path> -v`；测试需本地 PG 测试库（默认 `postgresql+asyncpg://nodeskclaw:nodeskclaw123@localhost:5432/nodeskclaw_rbac_test`，可用 `TEST_DATABASE_URL` 覆盖）
- 响应统一 `ApiResponse`（`{code:0, message:"success", data}`）；错误用 `app.core.exceptions` 的 `NotFoundError`/`BadRequestError`
- Gene 按 slug 查询一律 `.scalars().first()`（同 slug 多行坑，禁 `scalar_one_or_none()`）
- 对外序列化绝不包含 `created_by / org_id / lineage_group_id / parent_gene_id / created_by_instance_id / review_status`
- 代码禁止 emoji；注释/commit 中文；commit 标题 `<type>(<scope>): <中文描述>`；`git add` 只加本任务文件，禁止 `git add -A`（commit 前需用户确认）
- 定位代码用类/函数/文件名，不用行号
- 测试数据用 uuid 后缀保证 slug/name 唯一；建数据模式参照 `tests/test_admin_gene_route_auth.py`（`from tests.conftest import TestSessionLocal` + `async with TestSessionLocal() as db`）

---

### Task 1: 抽取技能 ZIP 打包 helper

**Files:**
- Modify: `nodeskclaw-backend/app/services/skill_package_service.py`（新增 `_manifest_bytes` / `build_gene_zip`）
- Modify: `nodeskclaw-backend/app/api/genes.py`（`download_gene` 改调 helper，删除 `_to_bytes` 与内联 ZIP 逻辑）
- Test: `nodeskclaw-backend/tests/test_skill_zip_helper.py`（新建）

**Interfaces:**
- Consumes: `skill_package_service.is_binary_entry(value) -> bool`、`decode_binary_entry(value) -> bytes`（已存在）
- Produces: `skill_package_service.build_gene_zip(gene) -> tuple[io.BytesIO, int]`——返回 `(buf, zip_size)`，buf 位置已 reset 到 0；manifest 损坏时抛 `json.JSONDecodeError`（调用方负责转 BadRequestError）

- [ ] **Step 1: 写失败测试**

```python
"""build_gene_zip 单测：ZIP 结构（SKILL.md/scripts/assets/references）、路径安全、二进制条目。"""

import io
import json
import zipfile

from app.services.skill_package_service import BINARY_MARKER, build_gene_zip


class _FakeGene:
    """只带 build_gene_zip 所需字段的轻量替身。"""

    def __init__(self, slug: str, manifest: dict):
        self.slug = slug
        self.manifest = json.dumps(manifest, ensure_ascii=False)


def _names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


def test_build_gene_zip_contains_all_sections():
    gene = _FakeGene("demo-skill", {
        "skill": {"content": "# Demo\n说明"},
        "scripts": {"main.py": "print('hi')", "nested/evil.py": "x"},
        "assets": {"assets/data.json": "{}", "../escape.txt": "bad"},
        "references": {"references/guide.md": "guide"},
    })
    buf, size = build_gene_zip(gene)
    data = buf.getvalue()
    assert size == len(data)
    names = _names(data)
    assert "demo-skill/SKILL.md" in names
    assert "demo-skill/main.py" in names              # scripts 取 basename
    assert "demo-skill/nested/evil.py" not in names   # basename 丢弃子目录
    assert "demo-skill/assets/data.json" in names
    assert "demo-skill/references/guide.md" in names
    assert not any("escape" in n for n in names)      # .. 路径被拒


def test_build_gene_zip_decodes_binary_entry():
    import base64

    raw = b"PK\x03\x04binary"
    entry = {BINARY_MARKER: True, "b64": base64.b64encode(raw).decode("ascii")}
    gene = _FakeGene("bin-skill", {"skill": {"content": "x"}, "assets": {"assets/a.docx": entry}})
    buf, _ = build_gene_zip(gene)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        assert zf.read("bin-skill/assets/a.docx") == raw


def test_build_gene_zip_corrupt_manifest_raises():
    class _Bad:
        slug = "bad"
        manifest = "{not-json"

    import pytest
    with pytest.raises(json.JSONDecodeError):
        build_gene_zip(_Bad())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_skill_zip_helper.py -v`
Expected: FAIL（`ImportError: cannot import name 'build_gene_zip'`）

- [ ] **Step 3: 实现 helper**

在 `skill_package_service.py` 中新增（确保模块顶部已含 `import io / import json / import posixpath / import zipfile`，已存在的跳过）：

```python
def _manifest_bytes(value) -> bytes:
    """将 manifest 字段值安全转换为 bytes，容忍 None 和非字符串类型。

    二进制 base64 条目（.docx 等）还原为原始字节。
    """
    if isinstance(value, bytes):
        return value
    if value is None:
        return b""
    if is_binary_entry(value):
        try:
            return decode_binary_entry(value)
        except ValueError:
            return b""
    return str(value).encode("utf-8")


def build_gene_zip(gene) -> tuple[io.BytesIO, int]:
    """将 Gene 的 manifest 打包为内存 ZIP，目录结构与 upload-folder 接口对称。

    供门户 /genes/{slug}/download 与开放 /registry download 两处复用。
    返回 (buf, zip_size)，buf 读取位置已 reset 到 0；manifest 损坏抛 json.JSONDecodeError。
    """
    manifest = json.loads(gene.manifest or "{}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # SKILL.md：来自 manifest.skill.content
        skill_content: str = manifest.get("skill", {}).get("content", "")
        zf.writestr(f"{gene.slug}/SKILL.md", _manifest_bytes(skill_content))

        # scripts：键为纯文件名（如 main.py），放在 {slug}/ 根目录
        for fname, content in manifest.get("scripts", {}).items():
            safe_name = posixpath.basename(fname)
            if safe_name and safe_name != ".":
                zf.writestr(f"{gene.slug}/{safe_name}", _manifest_bytes(content))

        # assets / references：键为带子目录的相对路径，拒绝 .. 逃逸
        for section in ("assets", "references"):
            for rel_path, content in manifest.get(section, {}).items():
                safe_path = posixpath.normpath(rel_path).lstrip("/")
                if ".." not in safe_path.split("/"):
                    zf.writestr(f"{gene.slug}/{safe_path}", _manifest_bytes(content))

    buf.seek(0, 2)
    zip_size = buf.tell()
    buf.seek(0)
    return buf, zip_size
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_skill_zip_helper.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 5: 门户 download 端点改调 helper**

在 `app/api/genes.py` 的 `download_gene` 函数中：删除模块级 `_to_bytes` 函数与函数体内联的 ZIP 构建块（从 `manifest: dict = json.loads(...)` 到 `buf.seek(0)` 的整段），改为：

```python
    try:
        buf, zip_size = skill_package_service.build_gene_zip(gene)
    except json.JSONDecodeError:
        from app.core.exceptions import BadRequestError
        raise BadRequestError("技能数据格式损坏，无法下载", "errors.gene.manifest_corrupt")
```

（`skill_package_service` 的 import 已在文件 `_to_bytes` 中局部出现——改为在文件顶部 `from app.services import skill_package_service`，并删除局部 import。`record_event` 埋点与 `StreamingResponse` 返回保持不变。重构后若 `genes.py` 顶部 `zipfile`/`posixpath` 不再被使用，按 ruff 提示一并删除。）

- [ ] **Step 6: 回归验证**

Run: `uv run pytest tests/test_skill_zip_helper.py -v && uv run ruff check app/services/skill_package_service.py app/api/genes.py`
Expected: 全 PASS，ruff 无报错

- [ ] **Step 7: Commit**

```bash
git add app/services/skill_package_service.py app/api/genes.py tests/test_skill_zip_helper.py
git commit -m "refactor(backend): 技能 ZIP 打包抽取为 skill_package_service.build_gene_zip 供门户与开放面复用"
```

---

### Task 2: 开放 Registry 服务层（public-only 查询与脱敏）

**Files:**
- Create: `nodeskclaw-backend/app/services/open_registry_service.py`
- Test: `nodeskclaw-backend/tests/test_open_registry_service.py`（新建）

**Interfaces:**
- Produces:
  - `open_registry_service.list_public_market_genes(db, *, keyword=None, tag=None, category=None, sort="popular", page=1, page_size=20) -> tuple[list[dict], int]`
  - `open_registry_service.get_public_market_gene_by_slug(db, slug) -> Gene | None`
  - `open_registry_service.list_public_tags(db) -> list[dict]`（`[{"tag": str, "count": int}]`，count 降序）
  - `open_registry_service.list_public_featured(db, *, limit=10) -> list[dict]`
  - `open_registry_service.public_gene_to_dict(gene) -> dict`（脱敏序列化）
  - `open_registry_service.EXPOSED_SEED_SLUGS = {"market-client"}`

- [ ] **Step 1: 写失败测试**

```python
"""开放 Registry 服务层测试：public-only 口径、种子隐藏与豁免、脱敏序列化、分页钳制。"""

import json
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
    visible = await _make_gene(install_count=5)
    await _make_gene(visibility="org_private")            # 组织私有
    await _make_gene(review_status="pending_admin")       # 待审
    await _make_gene(is_published=False)                  # 未发布
    async with TestSessionLocal() as db:
        items, _ = await list_public_market_genes(db, page_size=100)
    slugs = {i["slug"] for i in items}
    assert visible.slug in slugs


async def test_seed_hidden_but_exempt_slug_visible(monkeypatch):
    """官方种子默认隐藏，但豁免集合内的 slug 可见（机制测试，不绑死 market-client）。"""
    from app.services import open_registry_service as svc

    exempt_slug = f"exempt-{uuid4().hex[:8]}"
    monkeypatch.setattr(svc, "EXPOSED_SEED_SLUGS", {exempt_slug})
    hidden_prefix = f"seed-hidden-{uuid4().hex[:6]}"
    await _make_gene(source="official", slug=hidden_prefix)
    await _make_gene(source="official", slug=exempt_slug)
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
```

注意：`_make_gene(dependencies=...)` / `synergies=...` 走 `Gene(**{**defaults, **kw})` 透传，默认值里没有这两列（列默认 NULL），透传 JSON 字符串即可。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_open_registry_service.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.open_registry_service`）

- [ ] **Step 3: 实现服务层**

新建 `app/services/open_registry_service.py`：

```python
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

    base = select(Gene).where(_public_visible_filter())
    # 种子隐藏规则（与门户市场一致），豁免 EXPOSED_SEED_SLUGS
    base = base.where(
        not_(
            and_(
                Gene.source == "official",
                Gene.created_by.is_(None),
                Gene.slug.notin_(EXPOSED_SEED_SLUGS),
            )
        )
    )
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
    """聚合对外可见 gene 的标签。市场为百级规模，Python 侧聚合足够。"""
    result = await db.execute(
        select(Gene.tags).where(_public_visible_filter(), Gene.tags.isnot(None))
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_open_registry_service.py -v && uv run ruff check app/services/open_registry_service.py`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/open_registry_service.py tests/test_open_registry_service.py
git commit -m "feat(backend): 开放 Registry 服务层 public-only 查询与脱敏序列化"
```

---

### Task 3: IP 限流模块与 settings 开关

**Files:**
- Create: `nodeskclaw-backend/app/core/open_registry_rate_limit.py`
- Modify: `nodeskclaw-backend/app/core/config.py`（Settings 类新增 3 项，加在 `SEED_GENES` 附近）
- Test: `nodeskclaw-backend/tests/test_open_registry_rate_limit.py`（新建）

**Interfaces:**
- Consumes: `app.core.auth_rate_limit.get_client_ip(request) -> str`（已存在）、`app.core.exceptions.AppException`
- Produces:
  - `open_registry_rate_limit.check_open_registry_rate_limit(request, bucket="read") -> None`（超限抛 `OpenRegistryRateLimitError`，429）
  - `open_registry_rate_limit.reset_open_registry_rate_limits() -> None`（测试辅助）
  - settings：`OPEN_REGISTRY_ENABLED: bool = True`、`OPEN_REGISTRY_READ_RATE_LIMIT: int = 60`、`OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT: int = 30`

- [ ] **Step 1: 写失败测试**

```python
"""开放 Registry IP 限流单测：分桶计数、X-Forwarded-For、阈值来自 settings。"""

import pytest

from app.core import open_registry_rate_limit as rl


class _Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class _Client:
    def __init__(self, ip):
        self.host = ip


class _StubRequest:
    def __init__(self, ip="10.0.0.1", forwarded=None):
        self.headers = _Headers()
        if forwarded:
            self.headers["x-forwarded-for"] = forwarded
        self.client = _Client(ip)


@pytest.fixture(autouse=True)
def _reset():
    rl.reset_open_registry_rate_limits()
    yield
    rl.reset_open_registry_rate_limits()


def test_under_limit_passes():
    req = _StubRequest()
    for _ in range(3):
        rl.check_open_registry_rate_limit(req, bucket="read")


def test_over_limit_raises_429(monkeypatch):
    from app.core.exceptions import AppException

    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 3)
    req = _StubRequest()
    for _ in range(3):
        rl.check_open_registry_rate_limit(req, bucket="read")
    with pytest.raises(AppException) as ei:
        rl.check_open_registry_rate_limit(req, bucket="read")
    assert ei.value.status_code == 429


def test_buckets_are_independent(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1)
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT", 1)
    req = _StubRequest()
    rl.check_open_registry_rate_limit(req, bucket="read")
    rl.check_open_registry_rate_limit(req, bucket="download")
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(req, bucket="read")
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(req, bucket="download")


def test_forwarded_for_first_segment_used(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1)
    rl.check_open_registry_rate_limit(_StubRequest(forwarded="1.1.1.1, 2.2.2.2"))
    # 不同 IP 不受影响
    rl.check_open_registry_rate_limit(_StubRequest(forwarded="3.3.3.3, 2.2.2.2"))
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(_StubRequest(forwarded="1.1.1.1, 9.9.9.9"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_open_registry_rate_limit.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

新建 `app/core/open_registry_rate_limit.py`：

```python
"""开放 Registry 匿名限流：IP 维度进程内滑动窗口（写法参照 auth_rate_limit.py）。

不引入 Redis；多副本下计数不跨 Pod 共享（日活 ~150 规模 1-2 副本可接受，
与登录限流同款限制）。阈值从 settings 读取，环境变量可调。
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request

from app.core.auth_rate_limit import get_client_ip
from app.core.config import settings
from app.core.exceptions import AppException

_WINDOW_SECONDS = 60.0

# bucket -> ip -> 窗口期内时间戳列表（time.monotonic()）
_counters: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))


class OpenRegistryRateLimitError(AppException):
    def __init__(self):
        super().__init__(
            code=42900,
            message="请求过于频繁，请稍后再试",
            status_code=429,
            message_key="errors.common.too_many_attempts",
        )


def _limit_for(bucket: str) -> int:
    if bucket == "download":
        return settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT
    return settings.OPEN_REGISTRY_READ_RATE_LIMIT


def check_open_registry_rate_limit(request: Request, bucket: str = "read") -> None:
    """未超限即记账放行；超限抛 429。"""
    limit = _limit_for(bucket)
    key = get_client_ip(request)
    now = time.monotonic()
    timestamps = _counters[bucket][key]
    cutoff = now - _WINDOW_SECONDS
    timestamps[:] = [t for t in timestamps if t > cutoff]
    if len(timestamps) >= limit:
        raise OpenRegistryRateLimitError()
    timestamps.append(now)


def reset_open_registry_rate_limits() -> None:
    """测试辅助：清空全部计数。"""
    _counters.clear()
```

在 `app/core/config.py` 的 `Settings` 类中（`SEED_GENES` 附近）新增：

```python
    # ── 开放 Registry（/registry 匿名只读技能市场）──
    OPEN_REGISTRY_ENABLED: bool = True
    OPEN_REGISTRY_READ_RATE_LIMIT: int = 60      # 读类接口 次/分钟/IP
    OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT: int = 30  # 下载接口 次/分钟/IP
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_open_registry_rate_limit.py -v && uv run ruff check app/core/open_registry_rate_limit.py app/core/config.py`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/open_registry_rate_limit.py app/core/config.py tests/test_open_registry_rate_limit.py
git commit -m "feat(backend): 开放 Registry IP 滑动窗口限流与配置开关"
```

---

### Task 4: 开放 Registry router（6 端点 + 挂载 + 开关门）

**Files:**
- Create: `nodeskclaw-backend/app/api/open_registry.py`
- Modify: `nodeskclaw-backend/app/main.py`（Routers 区块挂载；`_gene_files` 列表本任务不动，Task 6 处理）
- Test: `nodeskclaw-backend/tests/test_open_registry.py`（新建）

**Interfaces:**
- Consumes: Task 1 `build_gene_zip`、Task 2 全部服务函数、Task 3 `check_open_registry_rate_limit` 与 settings；`app.core.deps.get_db`；`app.services.gene_market_stat_service.record_event(gene=, event_type=, db=, user_id=, org_id=, target_scope=)`
- Produces: 已挂载的 `/registry/api/v1/genes`、`/registry/api/v1/genes/tags`、`/registry/api/v1/genes/featured`、`/registry/api/v1/genes/{slug}`、`/registry/api/v1/genes/{slug}/manifest`、`/registry/api/v1/genes/{slug}/download`

- [ ] **Step 1: 写失败测试**

```python
"""开放 Registry API 测试：匿名可达、口径过滤、脱敏、ZIP 下载、限流 429、开关 404、路由顺序。"""

import json
import zipfile
import io
from uuid import uuid4

import pytest

from app.core import open_registry_rate_limit as rl
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
    visible = await _make_gene()
    await _make_gene(visibility="org_private")
    await _make_gene(review_status="pending_admin")
    await _make_gene(is_published=False)
    r = await client.get("/registry/api/v1/genes", params={"page_size": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    slugs = {i["slug"] for i in body["data"]["items"]}
    assert visible.slug in slugs


async def test_list_hides_official_seeds_except_market_client(client):
    await _make_gene(source="official", slug=f"seed-x-{uuid4().hex[:6]}")
    await _make_gene(source="official", slug="market-client",
                     manifest={"skill": {"content": "# bootstrap"}})
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
    assert (await client.get("/registry/api/v1/genes")).status_code == 429


async def test_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_ENABLED", False)
    for path in ("/registry/api/v1/genes",
                 "/registry/api/v1/genes/tags",
                 "/registry/api/v1/genes/featured"):
        assert (await client.get(path)).status_code == 404, path
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_open_registry.py -v`
Expected: FAIL（404——路由不存在）

- [ ] **Step 3: 实现 router**

新建 `app/api/open_registry.py`：

```python
"""开放 Registry：匿名只读的对外技能市场 API（GeneHub 协议形状）。

挂载于 /registry 前缀（main.py），供外部智能体平台（WorkBuddy 等）与其他
DeskClaw 平台（作为 GeneHub 协议 registry 源，GENEHUB_REGISTRY_URL 指向
https://<host>/registry）消费。数据口径见 open_registry_service 模块注释。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.open_registry_rate_limit import check_open_registry_rate_limit
from app.schemas.common import ApiResponse
from app.services import (
    gene_market_stat_service,
    open_registry_service,
    skill_package_service,
)

router = APIRouter()


async def _require_open_registry_enabled() -> None:
    """总开关：关闭时整个开放面 404（被滥用时运维一键止血）。"""
    if not settings.OPEN_REGISTRY_ENABLED:
        raise NotFoundError("资源不存在")


async def _read_limit(request: Request) -> None:
    check_open_registry_rate_limit(request, bucket="read")


async def _download_limit(request: Request) -> None:
    check_open_registry_rate_limit(request, bucket="download")


# 开关 + 读限流对所有端点生效；download 端点额外叠加下载限流
_open_router = APIRouter(
    dependencies=[Depends(_require_open_registry_enabled), Depends(_read_limit)]
)


def _get_visible_or_404(db: AsyncSession, slug: str):
    gene = await open_registry_service.get_public_market_gene_by_slug(db, slug)
    if gene is None:
        raise NotFoundError("技能不存在", "errors.gene.not_found")
    return gene


@_open_router.get("/api/v1/genes")
async def search_genes(
    q: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    sort: str = "popular",
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    items, total = await open_registry_service.list_public_market_genes(
        db, keyword=q, tag=tags, category=category,
        sort=sort, page=page, page_size=page_size,
    )
    return ApiResponse(data={"items": items, "total": total})


# 静态段必须声明在 {slug} 之前，否则被动态路由吞掉
@_open_router.get("/api/v1/genes/tags")
async def gene_tags(db: AsyncSession = Depends(get_db)):
    return ApiResponse(data=await open_registry_service.list_public_tags(db))


@_open_router.get("/api/v1/genes/featured")
async def featured_genes(limit: int = 10, db: AsyncSession = Depends(get_db)):
    return ApiResponse(
        data=await open_registry_service.list_public_featured(db, limit=limit)
    )


@_open_router.get("/api/v1/genes/{slug}")
async def gene_detail(slug: str, db: AsyncSession = Depends(get_db)):
    gene = await _get_visible_or_404(db, slug)
    return ApiResponse(data=open_registry_service.public_gene_to_dict(gene))


@_open_router.get("/api/v1/genes/{slug}/manifest")
async def gene_manifest(slug: str, db: AsyncSession = Depends(get_db)):
    gene = await _get_visible_or_404(db, slug)
    try:
        manifest = json.loads(gene.manifest or "{}")
    except json.JSONDecodeError:
        raise BadRequestError("技能数据格式损坏", "errors.gene.manifest_corrupt")
    await gene_market_stat_service.record_event(
        gene=gene, event_type="manifest_fetch", db=db,
        user_id=None, org_id=None, target_scope="open_registry",
    )
    return ApiResponse(data=manifest)


@_open_router.get(
    "/api/v1/genes/{slug}/download",
    dependencies=[Depends(_download_limit)],
)
async def gene_download(slug: str, db: AsyncSession = Depends(get_db)):
    gene = await _get_visible_or_404(db, slug)
    try:
        buf, zip_size = skill_package_service.build_gene_zip(gene)
    except json.JSONDecodeError:
        raise BadRequestError("技能数据格式损坏", "errors.gene.manifest_corrupt")
    await gene_market_stat_service.record_event(
        gene=gene, event_type="zip_download", db=db,
        user_id=None, org_id=None, target_scope="open_registry",
    )
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_slug}.zip"',
            "Content-Length": str(zip_size),
        },
    )


# 导出统一挂载入口
router.include_router(_open_router)
```

在 `app/main.py`：顶部 `from app.api.***` 导入区（搜索 `from app.api` 定位）加入 `from app.api.open_registry import router as open_registry_router`；Routers 区块（搜索 `app.include_router(webhook_router)`）其后一行加：

```python
app.include_router(open_registry_router, prefix="/registry")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_open_registry.py -v && uv run ruff check app/api/open_registry.py app/main.py`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/open_registry.py app/main.py tests/test_open_registry.py
git commit -m "feat(backend): 开放 Registry 匿名只读 API（/registry GeneHub 协议形状）"
```

---

### Task 5: llms.txt 自描述端点

**Files:**
- Modify: `nodeskclaw-backend/app/api/open_registry.py`（新增 `_LLM_TXT` 常量与 `/llms.txt` 路由）
- Test: `nodeskclaw-backend/tests/test_open_registry.py`（追加）

**Interfaces:**
- Produces: `GET /registry/llms.txt` → `text/markdown; charset=utf-8`

- [ ] **Step 1: 追加失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_open_registry.py::test_llms_txt -v`
Expected: FAIL（404）

- [ ] **Step 3: 实现**

在 `app/api/open_registry.py` 末尾（`router.include_router(_open_router)` 之前）加入：

```python
_LLM_TXT = """# DeskClaw Open Skill Registry

DeskClaw 平台的开放技能市场：匿名只读 REST API，提供公共已审技能的搜索、详情、
manifest 与 ZIP 下载。技能为 Agent Skills 开放格式（SKILL.md + scripts + assets + references）。

## 约定

- 无需鉴权；响应为 JSON：{"code": 0, "message": "success", "data": ...}
- 限流（按 IP）：读类 60 次/分钟，下载 30 次/分钟；超限返回 HTTP 429
- 仅含公共已审技能；组织/个人库与待审技能一律 404

## 端点

- GET /registry/api/v1/genes?q=&tags=&category=&sort=popular|rating|newest&page=1&page_size=20 — 搜索（page_size 上限 100）
- GET /registry/api/v1/genes/{slug} — 详情（市场元数据）
- GET /registry/api/v1/genes/{slug}/manifest — 技能本体（SKILL.md 全文/脚本/资源全部内联，二进制为 base64）
- GET /registry/api/v1/genes/{slug}/download — ZIP 下载（目录即 Agent Skills 格式）
- GET /registry/api/v1/genes/tags — 标签聚合
- GET /registry/api/v1/genes/featured?limit=10 — 精选

## 快速开始

    # 1. 搜索技能
    curl "https://<host>/registry/api/v1/genes?q=report"

    # 2. 推荐先装引导技能（教会你的 agent 使用任意 DeskClaw registry）
    curl -OJ "https://<host>/registry/api/v1/genes/market-client/download"

    # 3. 解压到 agent 的 skills 目录后按 SKILL.md 使用
"""


@_open_router.get("/llms.txt")
async def llms_txt():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(_LLM_TXT, media_type="text/markdown; charset=utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_open_registry.py -v && uv run ruff check app/api/open_registry.py`
Expected: 全 PASS（新测试 + 原有全过）

- [ ] **Step 5: Commit**

```bash
git add app/api/open_registry.py tests/test_open_registry.py
git commit -m "feat(backend): 开放 Registry llms.txt 自描述"
```

---

### Task 6: market-client 引导技能种子模板

**Files:**
- Create: `nodeskclaw-backend/app/data/gene_templates/market_client.json`
- Modify: `nodeskclaw-backend/app/main.py`（`_gene_files` 列表追加 `"market_client.json"`）
- Test: `nodeskclaw-backend/tests/test_open_registry.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `EXPOSED_SEED_SLUGS`（slug 必须恰为 `market-client` 才能进开放列表）
- Produces: 部署期随 `SEED_GENES` 幂等种子的官方引导技能（`source=official, visibility=public, review_status=approved, is_published=True`——由 main.py 种子逻辑统一设置）

- [ ] **Step 1: 追加失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_open_registry.py::test_market_client_template_valid -v`
Expected: FAIL（文件不存在）

- [ ] **Step 3: 创建模板文件**

新建 `app/data/gene_templates/market_client.json`：

```json
{
  "name": "market-client",
  "slug": "market-client",
  "description": "Bootstrap skill: teach any agent how to discover, search, download and install skills from a DeskClaw open skill registry (GeneHub protocol).",
  "category": "tools",
  "tags": ["market", "registry", "bootstrap", "skills"],
  "manifest": {
    "capabilities": ["research", "tooling"],
    "skill": {
      "name": "market-client",
      "content": "---\nname: market-client\ndescription: Discover and install skills from DeskClaw open skill registries\n---\n\nYou can use any DeskClaw-compatible open skill registry (GeneHub protocol) to discover and install new skills.\n\n## Registry discovery\n\nIf the registry host is not yet configured, ask the human for it. Then fetch the registry self-description:\n\n```bash\ncurl -s https://<registry-host>/registry/llms.txt\n```\n\nAll endpoints are relative to the registry host, require no auth, and return JSON shaped {\"code\": 0, \"data\": ...}.\n\n## Actions\n\n- `search` — GET /registry/api/v1/genes?q=KEYWORD&tags=TAG&sort=popular|rating|newest&page=1&page_size=20\n- `detail` — GET /registry/api/v1/genes/{slug}\n- `manifest` — GET /registry/api/v1/genes/{slug}/manifest (full skill body: SKILL.md text, scripts, assets; binaries are base64)\n- `download` — GET /registry/api/v1/genes/{slug}/download (same content as a ZIP)\n- `tags` — GET /registry/api/v1/genes/tags\n- `featured` — GET /registry/api/v1/genes/featured?limit=10\n\n## Usage Rules\n\n- Read the manifest and confirm a skill fits the task before installing; never install blindly.\n- Rate limits apply per IP (reads ~60/min, downloads ~30/min). On HTTP 429 wait 60 seconds, then retry.\n- HTTP 404 means the skill does not exist in this registry; retry `search` with different keywords before giving up.\n- Installing = download ZIP, unzip the {slug}/ folder (contains SKILL.md) into your skills directory, then follow SKILL.md.\n\n## Examples\n\n```bash\n# Search for report-writing skills\ncurl -s \"https://<registry-host>/registry/api/v1/genes?q=report\"\n\n# Download and install one\ncurl -OJ \"https://<registry-host>/registry/api/v1/genes/daily-report-writer/download\"\nunzip daily-report-writer.zip -d <skills-dir>/\n```"
    }
  }
}
```

在 `app/main.py` 种子导入的 `_gene_files` 列表（搜索 `_gene_files = [` 定位）末尾追加一项 `"market_client.json",`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_open_registry.py -v && uv run ruff check app/main.py`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/data/gene_templates/market_client.json app/main.py tests/test_open_registry.py
git commit -m "feat(backend): market-client 引导技能种子模板（对外技能市场引导）"
```

- [ ] **Step 6: 手动运维步骤（不由 AI 执行）**

提醒用户：官方种子新增后按仓库规则用 `scripts/upload_seeds_to_genehub.py` 推送 GeneHub（用户自行执行）。

---

### Task 7: README 文档

**Files:**
- Modify: `nodeskclaw-backend/README.md`（新增"开放 Registry API"一节，放在现有 API 说明附近）

**Interfaces:**
- Consumes: 前序任务的最终端点面

- [ ] **Step 1: 追加文档节**

在 `nodeskclaw-backend/README.md` 合适位置（API/端点说明之后）加入：

```markdown
## 开放 Registry API

平台可作为**开放数据提供方**对外提供公共技能市场，供外部智能体平台
（WorkBuddy 等）与其他 DeskClaw 实例消费。匿名只读，无需账号。

- 基址：`https://<host>/registry`，自描述文档：`GET /registry/llms.txt`
- 端点（GeneHub 协议形状）：
  - `GET /registry/api/v1/genes` 搜索（`q`/`tags`/`category`/`sort`/`page`/`page_size≤100`）
  - `GET /registry/api/v1/genes/{slug}` 详情
  - `GET /registry/api/v1/genes/{slug}/manifest` 技能本体（Agent Skills 格式，全量内联）
  - `GET /registry/api/v1/genes/{slug}/download` ZIP 下载
  - `GET /registry/api/v1/genes/tags` / `GET /registry/api/v1/genes/featured`
- 其他 DeskClaw 平台接入：`GENEHUB_REGISTRY_URL=https://<host>/registry`
- 数据范围：仅公共已审技能（组织/个人库不暴露）
- 限流（IP）：读 60 次/分钟、下载 30 次/分钟（`OPEN_REGISTRY_READ_RATE_LIMIT` /
  `OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT` 可调）；`OPEN_REGISTRY_ENABLED=false` 可整体关闭
```

- [ ] **Step 2: 验证文档一致性**

Run（在仓库根目录执行）: `python scripts/check_docs_consistency.py`
Expected: 通过（或输出与本改动无关的既有告警）

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(backend): README 补开放 Registry API 接入说明"
```

---

## 收尾验证（全量回归）

- [ ] `uv run pytest tests/test_open_registry.py tests/test_open_registry_service.py tests/test_open_registry_rate_limit.py tests/test_skill_zip_helper.py -v` 全 PASS
- [ ] `uv run ruff check .` 无新增报错
- [ ] 本地起服务后手工冒烟：`curl http://localhost:4510/registry/llms.txt`、`curl http://localhost:4510/registry/api/v1/genes?page_size=5`（服务启动需 `SEED_GENES=true` 让 market-client 落库）
