"""Organization settings endpoints -- required genes & SMTP configuration."""

import ipaddress
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks
from app.core.deps import get_db, require_feature, require_org_admin, require_org_member_role
from app.core.security import decrypt_sensitive, encrypt_sensitive
from app.models.base import not_deleted
from app.models.gene import Gene
from app.models.organization import Organization
from app.models.org_required_gene import OrgRequiredGene
from app.models.org_smtp_config import OrgSmtpConfig
from app.schemas.common import ApiResponse
from app.schemas.organization import OrgRequiredGeneAdd, OrgRequiredGeneInfo
from app.schemas.smtp import SmtpConfigCreate, SmtpConfigResponse, SmtpTestRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_info(rg: OrgRequiredGene, gene: Gene) -> OrgRequiredGeneInfo:
    return OrgRequiredGeneInfo(
        id=rg.id,
        gene_id=gene.id,
        gene_name=gene.name,
        gene_slug=gene.slug,
        gene_short_description=gene.short_description,
        gene_icon=gene.icon,
        gene_category=gene.category,
    )


@router.get(
    "/{org_id}/required-genes",
    response_model=ApiResponse[list[OrgRequiredGeneInfo]],
)
async def list_required_genes(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_member_role("member")),  # 读=member 及以上可访问
):
    result = await db.execute(
        select(OrgRequiredGene, Gene)
        .join(Gene, OrgRequiredGene.gene_id == Gene.id)
        .where(
            OrgRequiredGene.org_id == org_id,
            not_deleted(OrgRequiredGene),
            not_deleted(Gene),
        )
        .order_by(OrgRequiredGene.created_at)
    )
    rows = result.all()
    items = [_to_info(rg, gene) for rg, gene in rows]
    return ApiResponse(data=items)


@router.post(
    "/{org_id}/required-genes",
    response_model=ApiResponse[OrgRequiredGeneInfo],
)
async def add_required_gene(
    org_id: str,
    body: OrgRequiredGeneAdd,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_member_role("operator")),  # 写=operator 及以上可操作
):
    gene = await db.get(Gene, body.gene_id)
    if not gene or gene.deleted_at is not None:
        raise HTTPException(404, detail={
            "error_code": 40440,
            "message_key": "errors.gene.not_found",
            "message": "基因不存在",
        })

    existing = await db.execute(
        select(OrgRequiredGene).where(
            OrgRequiredGene.org_id == org_id,
            OrgRequiredGene.gene_id == body.gene_id,
            not_deleted(OrgRequiredGene),
        ).limit(1)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail={
            "error_code": 40901,
            "message_key": "errors.org_settings.gene_already_required",
            "message": "该基因已在默认工作基因列表中",
        })

    rg = OrgRequiredGene(org_id=org_id, gene_id=body.gene_id)
    db.add(rg)
    await db.commit()
    await db.refresh(rg)
    await hooks.emit("operation_audit", action="org.required_gene_added", target_type="organization", target_id=org_id, actor_id=_auth[0].id, org_id=org_id, details={"gene_id": body.gene_id})
    return ApiResponse(data=_to_info(rg, gene))


@router.delete(
    "/{org_id}/required-genes/{required_gene_id}",
    response_model=ApiResponse,
)
async def remove_required_gene(
    org_id: str,
    required_gene_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    result = await db.execute(
        select(OrgRequiredGene).where(
            OrgRequiredGene.id == required_gene_id,
            OrgRequiredGene.org_id == org_id,
            not_deleted(OrgRequiredGene),
        )
    )
    rg = result.scalar_one_or_none()
    if not rg:
        raise HTTPException(404, detail={
            "error_code": 40441,
            "message_key": "errors.org_settings.required_gene_not_found",
            "message": "默认工作基因记录不存在",
        })

    rg.soft_delete()
    await db.commit()
    await hooks.emit("operation_audit", action="org.required_gene_removed", target_type="organization", target_id=org_id, actor_id=_auth[0].id, org_id=org_id, details={"required_gene_id": required_gene_id})
    return ApiResponse(message="已移除")


# ── SMTP 配置 ─────────────────────────────────────────────


def _mask_password(encrypted: str) -> str:
    try:
        plain = decrypt_sensitive(encrypted)
        if len(plain) <= 3:
            return "****"
        return "****" + plain[-3:]
    except Exception:
        return "****"


def _smtp_to_response(cfg: OrgSmtpConfig) -> SmtpConfigResponse:
    return SmtpConfigResponse(
        id=cfg.id,
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_username=cfg.smtp_username,
        smtp_password_masked=_mask_password(cfg.smtp_password_encrypted),
        from_email=cfg.from_email,
        from_name=cfg.from_name,
        use_tls=cfg.use_tls,
    )


@router.get(
    "/{org_id}/smtp-config",
    response_model=ApiResponse[SmtpConfigResponse | None],
    dependencies=[Depends(require_feature("org_smtp_config"))],
)
async def get_smtp_config(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    result = await db.execute(
        select(OrgSmtpConfig).where(
            OrgSmtpConfig.org_id == org_id,
            not_deleted(OrgSmtpConfig),
        )
    )
    cfg = result.scalar_one_or_none()
    return ApiResponse(data=_smtp_to_response(cfg) if cfg else None)


@router.put(
    "/{org_id}/smtp-config",
    response_model=ApiResponse[SmtpConfigResponse],
    dependencies=[Depends(require_feature("org_smtp_config"))],
)
async def upsert_smtp_config(
    org_id: str,
    body: SmtpConfigCreate,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    result = await db.execute(
        select(OrgSmtpConfig).where(
            OrgSmtpConfig.org_id == org_id,
            not_deleted(OrgSmtpConfig),
        )
    )
    cfg = result.scalar_one_or_none()

    encrypted_pw = encrypt_sensitive(body.smtp_password)

    if cfg:
        cfg.smtp_host = body.smtp_host
        cfg.smtp_port = body.smtp_port
        cfg.smtp_username = body.smtp_username
        cfg.smtp_password_encrypted = encrypted_pw
        cfg.from_email = body.from_email
        cfg.from_name = body.from_name
        cfg.use_tls = body.use_tls
    else:
        cfg = OrgSmtpConfig(
            org_id=org_id,
            smtp_host=body.smtp_host,
            smtp_port=body.smtp_port,
            smtp_username=body.smtp_username,
            smtp_password_encrypted=encrypted_pw,
            from_email=body.from_email,
            from_name=body.from_name,
            use_tls=body.use_tls,
        )
        db.add(cfg)

    await db.commit()
    await db.refresh(cfg)
    await hooks.emit("operation_audit", action="org.smtp_config_updated", target_type="organization", target_id=org_id, actor_id=_auth[0].id, org_id=org_id)
    return ApiResponse(data=_smtp_to_response(cfg))


@router.delete(
    "/{org_id}/smtp-config",
    response_model=ApiResponse,
    dependencies=[Depends(require_feature("org_smtp_config"))],
)
async def delete_smtp_config(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    result = await db.execute(
        select(OrgSmtpConfig).where(
            OrgSmtpConfig.org_id == org_id,
            not_deleted(OrgSmtpConfig),
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, detail={
            "error_code": 40450,
            "message_key": "errors.smtp.config_not_found",
            "message": "SMTP 配置不存在",
        })
    cfg.soft_delete()
    await db.commit()
    return ApiResponse(message="SMTP 配置已删除")


@router.post(
    "/{org_id}/smtp-config/test",
    response_model=ApiResponse,
    dependencies=[Depends(require_feature("org_smtp_config"))],
)
async def test_smtp_config(
    org_id: str,
    body: SmtpTestRequest,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    result = await db.execute(
        select(OrgSmtpConfig).where(
            OrgSmtpConfig.org_id == org_id,
            not_deleted(OrgSmtpConfig),
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, detail={
            "error_code": 40450,
            "message_key": "errors.smtp.config_not_found",
            "message": "SMTP 配置不存在，请先保存配置",
        })

    from app.core.security import decrypt_sensitive
    from app.services.email.transport import SmtpConfig
    from app.services.email_service import send_test_email
    smtp = SmtpConfig(
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_username=cfg.smtp_username,
        smtp_password=decrypt_sensitive(cfg.smtp_password_encrypted),
        from_email=cfg.from_email,
        from_name=cfg.from_name,
        use_tls=cfg.use_tls,
    )
    try:
        await send_test_email(body.recipient_email, smtp)
    except Exception as exc:
        logger.warning("SMTP test failed for org %s: %s", org_id, exc)
        raise HTTPException(400, detail={
            "error_code": 40051,
            "message_key": "errors.smtp.test_failed",
            "message": f"SMTP 测试失败: {exc}",
        })

    return ApiResponse(message="测试邮件已发送")


# ── 外部智能体插件 SSRF 白名单（CIDR 列表，spec §9.2 / 任务 #5）───────────────


class ExternalAgentAllowedCidrsUpdate(BaseModel):
    """PUT 请求体：完整覆盖组织的 SSRF 白名单。"""

    cidrs: list[str] = Field(
        default_factory=list,
        description="CIDR 列表，如 ['10.0.0.0/8', '172.16.0.0/12']；空数组 = 拒绝所有私网",
    )


def _normalize_cidr(cidr: str) -> str | None:
    """校验并规范化一条 CIDR，返回规范化字符串（统一为字符串形式）；非法返回 None。"""
    if not isinstance(cidr, str):
        return None
    cidr = cidr.strip()
    if not cidr:
        return None
    # ipaddress.ip_network 接受 '10.0.0.0/8' 也接受裸 IP '10.0.0.1'（按 /32 处理）；
    # 业务上只接受显式 CIDR 形式，避免误把单点 IP 当作 CIDR 入库。
    if "/" not in cidr:
        return None
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    # 拒绝 /0（接受任意地址，等于关掉 SSRF 防护）
    if net.prefix_length == 0:
        return None
    return str(net)


@router.get(
    "/{org_id}/external-agent-allowed-cidrs",
    response_model=ApiResponse[list[str]],
)
async def get_external_agent_allowed_cidrs(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_member_role("operator")),
):
    """读取组织的外部智能体插件 SSRF 白名单（CIDR 列表）。

    operator 及以上可读——需要让插件提交者看到本组织允许的私网段。
    """
    result = await db.execute(
        select(Organization).where(
            Organization.id == org_id,
            not_deleted(Organization),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(404, detail={
            "error_code": 40460,
            "message_key": "errors.common.not_found",
            "message": "组织不存在",
        })
    return ApiResponse(data=list(org.external_agent_allowed_cidrs or []))


@router.put(
    "/{org_id}/external-agent-allowed-cidrs",
    response_model=ApiResponse[list[str]],
)
async def set_external_agent_allowed_cidrs(
    org_id: str,
    body: ExternalAgentAllowedCidrsUpdate,
    db: AsyncSession = Depends(get_db),
    _auth: tuple = Depends(require_org_admin),
):
    """设置组织的外部智能体插件 SSRF 白名单（CIDR 列表，覆盖式写入）。

    仅 org admin 可写（spec §9.2：威胁模型是管理员配置的 URL）。
    入参校验：每条 CIDR 必须是合法 IPv4/IPv6 CIDR、不能是 /0。
    即便允许了 169.254.0.0/16，云元数据地址 169.254.169.254 也仍被硬禁
    （defense-in-depth，详见 external_agent_ssrf 模块 _HARD_BLOCKED_HOSTS）。
    """
    result = await db.execute(
        select(Organization).where(
            Organization.id == org_id,
            not_deleted(Organization),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(404, detail={
            "error_code": 40460,
            "message_key": "errors.common.not_found",
            "message": "组织不存在",
        })

    normalized: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in body.cidrs:
        ok = _normalize_cidr(raw)
        if ok is None:
            invalid.append(raw)
            continue
        if ok in seen:
            continue
        seen.add(ok)
        normalized.append(ok)

    if invalid:
        raise HTTPException(400, detail={
            "error_code": 40060,
            "message_key": "errors.external_agent.ssrf_invalid_cidr",
            "message": f"非法 CIDR 条目: {invalid}",
            "invalid_cidrs": invalid,
        })

    org.external_agent_allowed_cidrs = normalized
    await db.commit()
    await db.refresh(org)
    await hooks.emit(
        "operation_audit",
        action="org.external_agent_allowed_cidrs_updated",
        target_type="organization",
        target_id=org_id,
        actor_id=_auth[0].id,
        org_id=org_id,
        details={"cidrs": normalized},
    )
    return ApiResponse(data=normalized)

