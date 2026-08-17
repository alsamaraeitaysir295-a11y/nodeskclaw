"""Instance template CRUD service."""

import json
import logging

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models.base import not_deleted
from app.models.gene import Gene, GeneReviewStatus, Genome, InstanceGene
from app.models.instance import Instance
from app.models.instance_template import InstanceTemplate, TemplateItem
from app.schemas.instance_template import (
    GeneRef,
    InstanceTemplateCreate,
    InstanceTemplateFromInstance,
    InstanceTemplateInfo,
    InstanceTemplateUpdate,
    TemplateItemInput,
    TemplateItemRef,
)
from app.services import gene_service

logger = logging.getLogger(__name__)


def _can_read_template(tpl: InstanceTemplate, org_id: str | None) -> bool:
    return tpl.visibility == "public" or tpl.org_id == org_id


def _can_manage_template(tpl: InstanceTemplate, org_id: str | None) -> bool:
    return tpl.org_id == org_id


async def _resolve_template_review_attrs(
    db: AsyncSession,
    target: str,
    *,
    user_id: str,
    org_id: str | None,
    is_super_admin: bool,
) -> dict:
    """根据 fork 目标 (personal/org/public) 派生模板行的归属字段。

    与 gene_service.resolve_target_attrs 保持一致的三段式：
      - personal：归属个人，无需审核，立即可用
      - org/public：归属组织，pending_owner 等组织 operator/admin 审核；
        操作者本身是目标 org 的 admin 或平台超管时 bypass，直接 approved
    """
    if target == "personal":
        return {
            "visibility": "personal",
            "org_id": None,
            "is_published": True,
            "review_status": None,
        }
    if target not in ("org", "public"):
        raise BadRequestError(f"未知 fork 目标: {target}")
    if not org_id:
        raise BadRequestError("org/public 目标必须提供 org_id")
    bypass = await gene_service.is_user_admin_of_org(
        db, user_id=user_id, org_id=org_id, is_super_admin=is_super_admin,
    )
    return {
        "visibility": "org_private" if target == "org" else "public",
        "org_id": org_id,
        "is_published": bypass,
        "review_status": GeneReviewStatus.approved if bypass else GeneReviewStatus.pending_owner,
    }


async def _get_template_model(
    db: AsyncSession,
    template_id: str,
    org_id: str | None,
    *,
    require_manage: bool = False,
) -> InstanceTemplate:
    result = await db.execute(
        select(InstanceTemplate).where(InstanceTemplate.id == template_id, not_deleted(InstanceTemplate))
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise NotFoundError("AI 员工模板不存在")
    if require_manage:
        if not _can_manage_template(tpl, org_id):
            raise NotFoundError("AI 员工模板不存在")
    elif not _can_read_template(tpl, org_id):
        raise NotFoundError("AI 员工模板不存在")
    return tpl


def _parse_gene_slugs(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


async def _resolve_gene_refs(db: AsyncSession, slugs: list[str]) -> list[GeneRef]:
    if not slugs:
        return []
    result = await db.execute(
        select(Gene).where(Gene.slug.in_(slugs), not_deleted(Gene))
    )
    gene_map = {g.slug: g for g in result.scalars().all()}
    return [
        GeneRef(
            slug=s,
            name=gene_map[s].name if s in gene_map else s,
            short_description=gene_map[s].short_description if s in gene_map else None,
            category=gene_map[s].category if s in gene_map else None,
            icon=gene_map[s].icon if s in gene_map else None,
        )
        for s in slugs
    ]


async def _resolve_item_refs(
    db: AsyncSession, items: list[TemplateItem],
) -> list[TemplateItemRef]:
    if not items:
        return []

    gene_slugs = [i.item_slug for i in items if i.item_type == "gene"]
    genome_slugs = [i.item_slug for i in items if i.item_type == "genome"]

    gene_map: dict[str, Gene] = {}
    if gene_slugs:
        res = await db.execute(
            select(Gene).where(Gene.slug.in_(gene_slugs), not_deleted(Gene))
        )
        gene_map = {g.slug: g for g in res.scalars().all()}

    genome_map: dict[str, Genome] = {}
    if genome_slugs:
        res = await db.execute(
            select(Genome).where(Genome.slug.in_(genome_slugs), not_deleted(Genome))
        )
        genome_map = {g.slug: g for g in res.scalars().all()}

    refs: list[TemplateItemRef] = []
    for item in items:
        if item.item_type == "gene":
            g = gene_map.get(item.item_slug)
            refs.append(TemplateItemRef(
                type="gene",
                slug=item.item_slug,
                name=g.name if g else item.item_slug,
                short_description=g.short_description if g else None,
                icon=g.icon if g else None,
            ))
        else:
            gm = genome_map.get(item.item_slug)
            gene_count = None
            if gm:
                try:
                    gene_count = len(json.loads(gm.gene_slugs or "[]"))
                except (json.JSONDecodeError, TypeError):
                    gene_count = 0
            refs.append(TemplateItemRef(
                type="genome",
                slug=item.item_slug,
                name=gm.name if gm else item.item_slug,
                short_description=gm.short_description if gm else None,
                icon=gm.icon if gm else None,
                gene_count=gene_count,
            ))
    return refs


async def _get_template_items(db: AsyncSession, template_id: str) -> list[TemplateItem]:
    result = await db.execute(
        select(TemplateItem)
        .where(TemplateItem.template_id == template_id, not_deleted(TemplateItem))
        .order_by(TemplateItem.sort_order)
    )
    return list(result.scalars().all())


async def _write_template_items(
    db: AsyncSession, template_id: str, inputs: list[TemplateItemInput],
) -> None:
    for idx, inp in enumerate(inputs):
        db.add(TemplateItem(
            template_id=template_id,
            item_type=inp.type,
            item_slug=inp.slug,
            sort_order=idx,
        ))


async def _soft_delete_template_items(db: AsyncSession, template_id: str) -> None:
    existing = await _get_template_items(db, template_id)
    for item in existing:
        item.soft_delete()


def _template_to_info(
    tpl: InstanceTemplate,
    genes: list[GeneRef] | None = None,
    item_refs: list[TemplateItemRef] | None = None,
) -> InstanceTemplateInfo:
    items = item_refs or []
    gene_slugs_from_items = [r.slug for r in items if r.type == "gene"]
    legacy_slugs = gene_slugs_from_items if items else _parse_gene_slugs(tpl.gene_slugs)

    return InstanceTemplateInfo(
        id=tpl.id,
        name=tpl.name,
        slug=tpl.slug,
        description=tpl.description,
        short_description=tpl.short_description,
        icon=tpl.icon,
        gene_slugs=legacy_slugs,
        genes=genes or [],
        items=items,
        source_instance_id=tpl.source_instance_id,
        is_published=tpl.is_published,
        is_featured=tpl.is_featured,
        use_count=tpl.use_count,
        created_by=tpl.created_by,
        org_id=tpl.org_id,
        visibility=tpl.visibility,
        review_status=tpl.review_status,
        created_at=tpl.created_at,
    )


async def list_templates(
    db: AsyncSession,
    *,
    org_id: str | None = None,
    visibility: str | None = None,
    keyword: str | None = None,
    featured_only: bool = False,
    page: int = 1,
    page_size: int = 20,
    requesting_user_id: str | None = None,
) -> tuple[list[InstanceTemplateInfo], int]:
    q = select(InstanceTemplate).where(not_deleted(InstanceTemplate), InstanceTemplate.is_published.is_(True))
    if visibility == "personal":
        # 个人库严格按 created_by 过滤；is_published 恒为 True（个人库免审），
        # requesting_user_id 为空时 created_by == None 恒不匹配，天然返回空列表，
        # 不会越权浏览他人个人库
        q = q.where(InstanceTemplate.visibility == "personal", InstanceTemplate.created_by == requesting_user_id)
    elif visibility == "org_private":
        q = q.where(InstanceTemplate.visibility == "org_private", InstanceTemplate.org_id == org_id)
    elif visibility == "public":
        q = q.where(InstanceTemplate.visibility == "public")
    elif org_id:
        q = q.where(
            or_(InstanceTemplate.visibility == "public", and_(InstanceTemplate.visibility == "org_private", InstanceTemplate.org_id == org_id))
        )
    if keyword:
        like = f"%{keyword}%"
        q = q.where(InstanceTemplate.name.ilike(like) | InstanceTemplate.description.ilike(like))
    if featured_only:
        q = q.where(InstanceTemplate.is_featured.is_(True))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(InstanceTemplate.use_count.desc(), InstanceTemplate.created_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    templates = result.scalars().all()

    items_list = [_template_to_info(t) for t in templates]
    return items_list, total


async def get_template(db: AsyncSession, template_id: str, org_id: str | None = None) -> InstanceTemplateInfo:
    tpl = await _get_template_model(db, template_id, org_id)
    ti_list = await _get_template_items(db, template_id)
    if ti_list:
        item_refs = await _resolve_item_refs(db, ti_list)
        gene_slugs = [r.slug for r in item_refs if r.type == "gene"]
        genes = await _resolve_gene_refs(db, gene_slugs)
        return _template_to_info(tpl, genes, item_refs)

    slugs = _parse_gene_slugs(tpl.gene_slugs)
    genes = await _resolve_gene_refs(db, slugs)
    return _template_to_info(tpl, genes)


async def get_template_gene_slugs(
    db: AsyncSession, template_id: str, org_id: str | None = None,
) -> list[str]:
    tpl = await _get_template_model(db, template_id, org_id)
    ti_list = await _get_template_items(db, template_id)
    if not ti_list:
        return _parse_gene_slugs(tpl.gene_slugs)

    all_slugs: list[str] = []
    genome_slugs_to_expand = []
    for item in ti_list:
        if item.item_type == "gene":
            all_slugs.append(item.item_slug)
        else:
            genome_slugs_to_expand.append(item.item_slug)

    if genome_slugs_to_expand:
        res = await db.execute(
            select(Genome).where(Genome.slug.in_(genome_slugs_to_expand), not_deleted(Genome))
        )
        found_slugs: set[str] = set()
        for gm in res.scalars().all():
            found_slugs.add(gm.slug)
            try:
                expanded = json.loads(gm.gene_slugs or "[]")
                all_slugs.extend(expanded)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse gene_slugs for genome %s", gm.slug)
        missing = set(genome_slugs_to_expand) - found_slugs
        if missing:
            logger.warning("Genomes not found during expansion: %s", missing)

    seen: set[str] = set()
    deduped: list[str] = []
    for s in all_slugs:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


async def create_template(
    db: AsyncSession,
    req: InstanceTemplateCreate,
    user_id: str,
) -> InstanceTemplateInfo:
    # \u76f4\u63a5\u521b\u5efa\u7edf\u4e00\u843d\u4e2a\u4eba\u5e93\uff08\u5bf9\u9f50 /genes/manual \u7684\u4ea7\u54c1\u51b3\u7b56\uff1a\u7ec4\u7ec7/\u5e02\u573a\u5185\u5bb9\u4e00\u5f8b
    # \u8d70 fork \u540c\u6b65\uff0c\u4e0d\u5141\u8bb8\u76f4\u63a5\u521b\u5efa\uff09\uff0c\u67e5\u91cd\u6309\u4e2a\u4eba\u5e93\u8303\u56f4\uff08\u540c\u4e00 slug \u4e0d\u540c\u7528\u6237\u53ef\u5e76\u5b58\uff09
    existing = await db.execute(
        select(InstanceTemplate).where(
            InstanceTemplate.slug == req.slug,
            InstanceTemplate.org_id.is_(None),
            InstanceTemplate.created_by == user_id,
            not_deleted(InstanceTemplate),
        )
    )
    if existing.scalar_one_or_none():
        raise ConflictError(f"\u6a21\u677f slug '{req.slug}' \u5df2\u5b58\u5728")

    items_input = req.items
    if not items_input and req.gene_slugs:
        items_input = [TemplateItemInput(type="gene", slug=s) for s in req.gene_slugs]

    tpl = InstanceTemplate(
        name=req.name,
        slug=req.slug,
        description=req.description,
        short_description=req.short_description,
        icon=req.icon,
        gene_slugs=json.dumps([i.slug for i in items_input if i.type == "gene"]) if items_input else "[]",
        created_by=user_id,
        org_id=None,
        visibility="personal",
        is_published=True,
        review_status=None,
    )
    db.add(tpl)
    await db.flush()

    if items_input:
        await _write_template_items(db, tpl.id, items_input)

    await db.commit()
    await db.refresh(tpl)

    ti_list = await _get_template_items(db, tpl.id)
    item_refs = await _resolve_item_refs(db, ti_list)
    gene_slugs = [r.slug for r in item_refs if r.type == "gene"]
    genes = await _resolve_gene_refs(db, gene_slugs)
    return _template_to_info(tpl, genes, item_refs)


async def create_from_instance(
    db: AsyncSession,
    instance_id: str,
    req: InstanceTemplateFromInstance,
    user_id: str,
    org_id: str | None = None,
) -> InstanceTemplateInfo:
    inst = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.org_id == org_id,
            not_deleted(Instance),
        )
    )
    if not inst.scalar_one_or_none():
        raise NotFoundError("实例不存在")

    # \u76f4\u63a5\u521b\u5efa\u7edf\u4e00\u843d\u4e2a\u4eba\u5e93\uff0c\u67e5\u91cd\u6309\u4e2a\u4eba\u5e93\u8303\u56f4\uff08\u540c\u4e00 slug \u4e0d\u540c\u7528\u6237\u53ef\u5e76\u5b58\uff09
    existing = await db.execute(
        select(InstanceTemplate).where(
            InstanceTemplate.slug == req.slug,
            InstanceTemplate.org_id.is_(None),
            InstanceTemplate.created_by == user_id,
            not_deleted(InstanceTemplate),
        )
    )
    if existing.scalar_one_or_none():
        raise ConflictError(f"\u6a21\u677f slug '{req.slug}' \u5df2\u5b58\u5728")

    ig_result = await db.execute(
        select(Gene.slug).join(InstanceGene, InstanceGene.gene_id == Gene.id).where(
            InstanceGene.instance_id == instance_id,
            InstanceGene.status == "installed",
            not_deleted(InstanceGene),
            not_deleted(Gene),
        )
    )
    gene_slugs = [row[0] for row in ig_result.all()]

    tpl = InstanceTemplate(
        name=req.name,
        slug=req.slug,
        description=req.description,
        short_description=req.short_description,
        icon=req.icon,
        gene_slugs=json.dumps(gene_slugs),
        source_instance_id=instance_id,
        created_by=user_id,
        org_id=None,
        visibility="personal",
        is_published=True,
        review_status=None,
    )
    db.add(tpl)
    await db.flush()

    items_input = [TemplateItemInput(type="gene", slug=s) for s in gene_slugs]
    await _write_template_items(db, tpl.id, items_input)

    await db.commit()
    await db.refresh(tpl)

    ti_list = await _get_template_items(db, tpl.id)
    item_refs = await _resolve_item_refs(db, ti_list)
    genes = await _resolve_gene_refs(db, gene_slugs)
    return _template_to_info(tpl, genes, item_refs)


async def fork_template_to_library(
    db: AsyncSession,
    source_template_id: str,
    target: str,
    *,
    current_user,  # app.models.user.User —— 取 id / is_super_admin / current_org_id
    org_id: str | None = None,
) -> InstanceTemplateInfo:
    """从任意 scope（personal / org / public）fork 一份模板到 personal / org / public 库。

    对照 gene_service.fork_gene_to_library 的非 overwrite 分支：创建一份新副本，
    原模板不受影响。目标 scope 内已有同名 slug 直接 ConflictError，不支持覆盖。

    权限规则（current_user.is_super_admin=True 时全部放行）：
      - 源是 personal：仅 source.created_by == current_user.id 可 fork
      - 源是 org（org_private）：current_user 必须是该 org 的有效成员
      - 源是 public：任意登录用户可 fork
    """
    source = (await db.execute(
        select(InstanceTemplate).where(InstanceTemplate.id == source_template_id, not_deleted(InstanceTemplate))
    )).scalar_one_or_none()
    if not source:
        raise NotFoundError("AI 员工模板不存在")

    if not getattr(current_user, "is_super_admin", False):
        if source.visibility == "personal":
            if source.created_by != current_user.id:
                raise ForbiddenError(
                    message="仅本人可 fork 自己的个人模板",
                    message_key="errors.instance_template.fork_personal_forbidden",
                )
        elif source.visibility == "org_private":
            from app.models.org_membership import OrgMembership

            membership = (await db.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == current_user.id,
                    OrgMembership.org_id == source.org_id,
                    OrgMembership.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if membership is None:
                raise ForbiddenError(
                    message="仅本组织成员可 fork 该组织模板",
                    message_key="errors.instance_template.fork_org_forbidden",
                )
        # visibility == "public"：任意登录用户均可，无须额外校验

    effective_org_id = org_id if org_id is not None else getattr(current_user, "current_org_id", None)
    attrs = await _resolve_template_review_attrs(
        db, target,
        user_id=current_user.id,
        org_id=effective_org_id,
        is_super_admin=getattr(current_user, "is_super_admin", False),
    )

    # 查重必须按 visibility 一起判断：org_private 和 public fork 到同一个 org 时
    # attrs["org_id"] 相同，仅按 org_id 查重会把两种不同 scope 的模板误判冲突。
    # public 的唯一性是全平台范围（不按 org_id 限定）；personal 的唯一性按
    # created_by 限定（不同用户各自的个人库互不冲突）——对齐 Gene 市场三态唯一索引。
    dup_conditions = [InstanceTemplate.slug == source.slug, not_deleted(InstanceTemplate)]
    if attrs["visibility"] == "public":
        dup_conditions.append(InstanceTemplate.visibility == "public")
    elif attrs["visibility"] == "personal":
        dup_conditions.append(InstanceTemplate.visibility == "personal")
        dup_conditions.append(InstanceTemplate.created_by == current_user.id)
    else:
        dup_conditions.append(InstanceTemplate.visibility == attrs["visibility"])
        dup_conditions.append(InstanceTemplate.org_id == attrs["org_id"])
    existing = await db.execute(select(InstanceTemplate).where(*dup_conditions))
    if existing.scalar_one_or_none():
        raise ConflictError(f"模板 slug '{source.slug}' 已存在")

    fork = InstanceTemplate(
        name=source.name,
        slug=source.slug,
        description=source.description,
        short_description=source.short_description,
        icon=source.icon,
        gene_slugs=source.gene_slugs,
        source_instance_id=source.source_instance_id,
        created_by=current_user.id,
        org_id=attrs["org_id"],
        visibility=attrs["visibility"],
        is_published=attrs["is_published"],
        review_status=attrs["review_status"],
    )
    db.add(fork)
    await db.flush()

    source_items = await _get_template_items(db, source.id)
    if source_items:
        items_input = [TemplateItemInput(type=it.item_type, slug=it.item_slug) for it in source_items]
        await _write_template_items(db, fork.id, items_input)

    await db.commit()
    await db.refresh(fork)

    ti_list = await _get_template_items(db, fork.id)
    item_refs = await _resolve_item_refs(db, ti_list)
    gene_slugs = [r.slug for r in item_refs if r.type == "gene"]
    genes = await _resolve_gene_refs(db, gene_slugs)
    return _template_to_info(fork, genes, item_refs)


async def update_template(
    db: AsyncSession,
    template_id: str,
    req: InstanceTemplateUpdate,
    org_id: str | None = None,
) -> InstanceTemplateInfo:
    tpl = await _get_template_model(db, template_id, org_id, require_manage=True)
    if req.name is not None:
        tpl.name = req.name
    if req.description is not None:
        tpl.description = req.description
    if req.short_description is not None:
        tpl.short_description = req.short_description
    if req.icon is not None:
        tpl.icon = req.icon

    items_input = req.items
    if items_input is None and req.gene_slugs is not None:
        items_input = [TemplateItemInput(type="gene", slug=s) for s in req.gene_slugs]

    if items_input is not None:
        await _soft_delete_template_items(db, template_id)
        await _write_template_items(db, template_id, items_input)
        tpl.gene_slugs = json.dumps([i.slug for i in items_input if i.type == "gene"])

    await db.commit()
    await db.refresh(tpl)

    ti_list = await _get_template_items(db, template_id)
    item_refs = await _resolve_item_refs(db, ti_list)
    gene_slugs = [r.slug for r in item_refs if r.type == "gene"]
    genes = await _resolve_gene_refs(db, gene_slugs)
    return _template_to_info(tpl, genes, item_refs)


async def delete_template(db: AsyncSession, template_id: str, org_id: str | None = None) -> dict:
    tpl = await _get_template_model(db, template_id, org_id, require_manage=True)
    await _soft_delete_template_items(db, template_id)
    tpl.soft_delete()
    await db.commit()
    return {"deleted": True}


async def increment_use_count(db: AsyncSession, template_id: str) -> None:
    result = await db.execute(
        select(InstanceTemplate).where(InstanceTemplate.id == template_id, not_deleted(InstanceTemplate))
    )
    tpl = result.scalar_one_or_none()
    if tpl:
        tpl.use_count = (tpl.use_count or 0) + 1
        await db.commit()


async def review_instance_template(
    db: AsyncSession,
    template_id: str,
    action: str,
    reason: str | None = None,
    *,
    current_user=None,
) -> dict:
    """审核 AI 员工模板，权限模型与 gene_service.review_gene 一致。

    权限：当前用户必须是该模板所属 org 的 OrgRole.operator/admin 或平台超管。
    状态机：pending_owner → approved（is_published=True）；任意状态 → rejected。
    """
    from fastapi import HTTPException, status

    from app.models.org_membership import OrgMembership, OrgRole

    result = await db.execute(select(InstanceTemplate).where(InstanceTemplate.id == template_id, not_deleted(InstanceTemplate)))
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise NotFoundError("模板不存在")

    if current_user is not None:
        allowed = False
        if getattr(current_user, "is_super_admin", False):
            allowed = True
        elif tpl.org_id:
            membership = (await db.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == current_user.id,
                    OrgMembership.org_id == tpl.org_id,
                    OrgMembership.role.in_([OrgRole.operator, OrgRole.admin]),
                    OrgMembership.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if membership:
                allowed = True
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": 40318,
                    "message_key": "errors.instance_template.review_forbidden",
                    "message": "您无权审核此模板（需该模板所属组织的管理员）",
                },
            )

    if action == "approve":
        if tpl.review_status in (GeneReviewStatus.pending_owner, GeneReviewStatus.pending_admin):
            tpl.review_status = GeneReviewStatus.approved
            tpl.is_published = True
        else:
            raise BadRequestError(f"当前审核状态 '{tpl.review_status}' 不可审核通过")
    elif action == "reject":
        tpl.review_status = GeneReviewStatus.rejected
        tpl.is_published = False
    else:
        raise BadRequestError(f"未知审核动作: {action}")

    await db.commit()
    return {"review_status": tpl.review_status, "is_published": tpl.is_published}


async def get_pending_review_instance_templates(db: AsyncSession, current_user=None) -> list[dict]:
    """获取当前用户可审核的待审模板列表，权限模型与 gene_service.get_pending_review_genes 一致。"""
    from app.models.org_membership import OrgMembership, OrgRole

    base_filter = InstanceTemplate.review_status.in_([GeneReviewStatus.pending_owner, GeneReviewStatus.pending_admin])

    if current_user is not None and not getattr(current_user, "is_super_admin", False):
        admin_orgs_result = await db.execute(
            select(OrgMembership.org_id).where(
                OrgMembership.user_id == current_user.id,
                OrgMembership.role.in_([OrgRole.operator, OrgRole.admin]),
                OrgMembership.deleted_at.is_(None),
            )
        )
        admin_org_ids = [row[0] for row in admin_orgs_result.all()]
        if not admin_org_ids:
            return []
        q = select(InstanceTemplate).where(base_filter, InstanceTemplate.org_id.in_(admin_org_ids), not_deleted(InstanceTemplate))
    else:
        # 超管（或未传 current_user 的兼容路径）→ 全部
        q = select(InstanceTemplate).where(base_filter, not_deleted(InstanceTemplate))

    rows = (await db.execute(q.order_by(InstanceTemplate.created_at.desc()))).scalars().all()
    items = [
        {
            "id": tpl.id,
            "name": tpl.name,
            "slug": tpl.slug,
            "visibility": tpl.visibility,
            "review_status": tpl.review_status,
            "created_by": tpl.created_by,
            "org_id": tpl.org_id,
            "created_at": tpl.created_at,
        }
        for tpl in rows
    ]
    return await _attach_uploader_identity(db, items)


async def _attach_uploader_identity(db: AsyncSession, items: list[dict]) -> list[dict]:
    """批量给模板 dict 注入 created_by_name / created_by_email，避免审核列表裸显 UUID。

    与 gene_service._attach_uploader_identity / org_join_request_service._attach_identity
    是同一套约定的独立实现（本仓库里各域各自维护一份，不跨域互相 import 私有函数）。
    """
    if not items:
        return items

    user_ids = {it.get("created_by") for it in items if it.get("created_by")}
    if not user_ids:
        return items

    from app.models.user import User

    rows = (await db.execute(
        select(User.id, User.name, User.email).where(User.id.in_(user_ids))
    )).all()
    by_id = {row[0]: (row[1], row[2]) for row in rows}

    for it in items:
        uid = it.get("created_by")
        if uid and uid in by_id:
            name, email = by_id[uid]
            it["created_by_name"] = name
            it["created_by_email"] = email
        else:
            it["created_by_name"] = None
            it["created_by_email"] = None
    return items
