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


async def _get_visible_or_404(db: AsyncSession, slug: str):
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
    except (json.JSONDecodeError, AttributeError):
        # AttributeError：manifest 为合法 JSON 但非 dict（如 "[1,2]"）时，
        # 下游 .get 会抛 AttributeError，与 JSON 损坏一并归一为 BadRequest
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
    except (json.JSONDecodeError, AttributeError):
        # 同上：manifest 非 dict 时 build_gene_zip 内 .get 抛 AttributeError
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


# import 时由 settings 格式化，限流调参后文档数字零漂移（措辞不变，仅数值动态）
_LLM_TXT = f"""# DeskClaw Open Skill Registry

DeskClaw 平台的开放技能市场：匿名只读 REST API，提供公共已审技能的搜索、详情、
manifest 与 ZIP 下载。技能为 Agent Skills 开放格式（SKILL.md + scripts + assets + references）。

## 约定

- 无需鉴权；响应为 JSON：{{"code": 0, "message": "success", "data": ...}}
- 限流（按 IP）：读类 {settings.OPEN_REGISTRY_READ_RATE_LIMIT} 次/分钟，下载 {settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT} 次/分钟；超限返回 HTTP 429
- 仅含公共已审技能；组织/个人库与待审技能一律 404

## 端点

- GET /registry/api/v1/genes?q=&tags=&category=&sort=popular|rating|newest&page=1&page_size=20 — 搜索（page_size 上限 100）
- GET /registry/api/v1/genes/{{slug}} — 详情（市场元数据）
- GET /registry/api/v1/genes/{{slug}}/manifest — 技能本体（SKILL.md 全文/脚本/资源全部内联，二进制为 base64）
- GET /registry/api/v1/genes/{{slug}}/download — ZIP 下载（目录即 Agent Skills 格式）
- GET /registry/api/v1/genes/tags — 标签聚合
- GET /registry/api/v1/genes/featured?limit=10 — 精选
- GET /registry/llms.txt — 本自描述文档

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


# 导出统一挂载入口
router.include_router(_open_router)
