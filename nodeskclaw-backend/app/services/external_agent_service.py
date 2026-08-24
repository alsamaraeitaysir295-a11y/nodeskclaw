"""ExternalAgent 的业务逻辑层：CRUD + API Key 加解密 + Function CRUD。"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.security import decrypt_sensitive, encrypt_sensitive
from app.models.base import not_deleted
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.organization import Organization

logger = logging.getLogger(__name__)


async def create_external_agent(
    org_id: str,
    name: str,
    endpoint: str | None,
    protocol: str | None,
    api_key: str | None,
    description: str | None,
    capabilities: list[str],
    icon_emoji: str | None,
    theme_color: str | None,
    db: AsyncSession,
    type: str | None = None,
    session_managed_by: str | None = None,
    invoke_config: dict | None = None,
    input_schema: dict | None = None,
    status: str | None = None,
) -> ExternalAgent:
    # tool 型 endpoint 为 None；落库前用 invoke_config.endpoint 兜底，
    # 因为 ExternalAgent.endpoint 列 NOT NULL（spec §5 / migration 未放宽）
    stored_endpoint = (endpoint or (invoke_config or {}).get("endpoint") or "").rstrip("/")

    # 加密 invoke_config.auth.token（与 function-level 路径同款；spec §9.1 要求所有
    # 密钥落库走 encrypt_sensitive；之前只加密了 api_key_encrypted，顶层 invoke_config
    # 里的 token 是明文，与 §13 #9 "密钥在任何响应/日志无明文" 冲突）
    if invoke_config and invoke_config.get("auth", {}).get("token"):
        invoke_config = {**invoke_config, "auth": {
            **invoke_config["auth"],
            "token": encrypt_sensitive(invoke_config["auth"]["token"]),
        }}

    agent = ExternalAgent(
        org_id=org_id,
        name=name,
        endpoint=stored_endpoint,
        protocol=protocol or "openai_compatible",
        api_key_encrypted=encrypt_sensitive(api_key) if api_key else None,
        description=description,
        capabilities=json.dumps(capabilities, ensure_ascii=False),
        icon_emoji=icon_emoji,
        theme_color=theme_color,
        type=type or "chat",
        session_managed_by=session_managed_by,
        invoke_config=invoke_config,
        input_schema=input_schema,
        status=status or "active",
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def list_external_agents(org_id: str, db: AsyncSession) -> list[ExternalAgent]:
    result = await db.execute(
        select(ExternalAgent)
        .where(ExternalAgent.org_id == org_id, ExternalAgent.deleted_at.is_(None))
        .order_by(ExternalAgent.created_at.desc())
    )
    return list(result.scalars().all())


async def get_external_agent(agent_id: str, org_id: str, db: AsyncSession) -> ExternalAgent:
    result = await db.execute(
        select(ExternalAgent).where(
            ExternalAgent.id == agent_id,
            ExternalAgent.org_id == org_id,
            ExternalAgent.deleted_at.is_(None),
        )
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("external_agent", agent_id)
    return agent


def get_decrypted_api_key(agent: ExternalAgent) -> str | None:
    """解密 API Key；未配置时返回 None。"""
    if not agent.api_key_encrypted:
        return None
    return decrypt_sensitive(agent.api_key_encrypted)


def get_capabilities(agent: ExternalAgent) -> list[str]:
    """从 JSON 字段解析能力标签列表。"""
    if not agent.capabilities:
        return []
    try:
        return json.loads(agent.capabilities)
    except Exception:
        return []


async def update_external_agent(
    agent_id: str,
    org_id: str,
    updates: dict,
    db: AsyncSession,
) -> ExternalAgent:
    agent = await get_external_agent(agent_id, org_id, db)

    # API Key 单独处理：需重新加密
    if "api_key" in updates:
        new_key = updates.pop("api_key")
        if new_key:
            agent.api_key_encrypted = encrypt_sensitive(new_key)

    # capabilities 列表转 JSON 字符串
    if "capabilities" in updates:
        updates["capabilities"] = json.dumps(updates["capabilities"], ensure_ascii=False)

    # endpoint 去除尾部斜杠（tool 型允许显式置空并回退到 invoke_config.endpoint）
    if "endpoint" in updates and updates["endpoint"]:
        updates["endpoint"] = updates["endpoint"].rstrip("/")
    elif "endpoint" in updates and not updates["endpoint"]:
        updates["endpoint"] = (agent.invoke_config or {}).get("endpoint", "") or ""
    elif "invoke_config" in updates:
        # 仅传 invoke_config 时同步刷新 endpoint 列（保持列非空）
        new_ep = (updates["invoke_config"] or {}).get("endpoint")
        if new_ep:
            updates["endpoint"] = new_ep.rstrip("/")

    # Manifest 变更触发 version+1，前端表单据此使缓存失效（spec §10）
    # 用 dict 比较即可，复杂嵌套场景下若需要精确 JSON diff 可改用 deepdiff，
    # 但目前 spec 要求只要有变更就 +1，简单比较已满足需求。
    schema_changed = (
        "invoke_config" in updates and updates["invoke_config"] != agent.invoke_config
    ) or (
        "input_schema" in updates and updates["input_schema"] != agent.input_schema
    )

    for key, value in updates.items():
        setattr(agent, key, value)

    if schema_changed:
        agent.version = (agent.version or 1) + 1

    await db.commit()
    await db.refresh(agent)
    return agent


async def delete_external_agent(agent_id: str, org_id: str, db: AsyncSession) -> None:
    agent = await get_external_agent(agent_id, org_id, db)
    agent.soft_delete()
    await db.commit()


def is_attachment_key_owned_by_org(storage_key: str, org_id: str) -> bool:
    """校验 storage_key 是否落在当前组织的外部 Agent 附件命名空间内。

    聊天端点接收的 attachments 由前端上报、不受信任——用户理论上可以在请求体里
    塞任意 storage_key（比如别的组织、别的功能模块上传的文件，甚至带 ../ 的路径），
    而服务端后续会拿这个 key 去签发预签名 / HMAC 下载 URL。必须在使用前确认这个
    key 真的属于"当前组织的外部 Agent 附件"这个命名空间，见
    storage_service.upload_external_agent_file 生成 key 时用的同一套前缀规则。
    """
    if ".." in storage_key.split("/"):
        return False
    key = storage_key
    prefix = (settings.S3_KEY_PREFIX or "").strip("/")
    if prefix and key.startswith(f"{prefix}/"):
        key = key[len(prefix) + 1:]
    return key.startswith(f"external-agent-files/{org_id}/")


# ── Function CRUD（spec §7.2）───────────────────────────────────────────────


async def _load_org_allowed_cidrs(org_id: str, db: AsyncSession) -> list[str]:
    """读取组织当前的 SSRF 白名单（CIDR 列表）。每次请求都重新读 DB，不缓存。

    与 api/external_agents.py 的同名函数重复（service 与 api 都能查到），便于
    service 层在没有 HTTP 上下文时（如后台任务）也能使用。
    """
    result = await db.execute(
        select(Organization.external_agent_allowed_cidrs).where(Organization.id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return []
    return list(row)


async def list_functions(
    *, agent_id: str, org_id: str, db: AsyncSession,
) -> list[ExternalAgentFunction]:
    """列出某 agent 下所有 function（按 sort_order 升序，未软删）。"""
    # 校验 agent.org_id == org_id（IDOR 防护；找不到 → 404）
    await get_external_agent(agent_id=agent_id, org_id=org_id, db=db)
    result = await db.execute(
        select(ExternalAgentFunction)
        .where(
            ExternalAgentFunction.agent_id == agent_id,
            not_deleted(ExternalAgentFunction),
        )
        .order_by(
            ExternalAgentFunction.sort_order.asc(),
            ExternalAgentFunction.created_at.asc(),
        )
    )
    return list(result.scalars().all())


async def get_function(
    *, function_id: str, org_id: str, db: AsyncSession,
) -> ExternalAgentFunction:
    """取 function 并校验归属（IDOR 防护）。

    - 不加 deleted_at 过滤：调用方可自行判断是否处理"已软删"；
    - 通过所属 agent 的 org_id 校验实现跨组织隔离。
    """
    result = await db.execute(
        select(ExternalAgentFunction).where(ExternalAgentFunction.id == function_id)
    )
    func = result.scalar_one_or_none()
    if func is None:
        raise NotFoundError("external_agent_function", function_id)
    # 校验所属 agent 在当前 org 内
    await get_external_agent(agent_id=func.agent_id, org_id=org_id, db=db)
    return func


async def get_default_function_for_agent(
    *, agent_id: str, org_id: str, db: AsyncSession,
) -> ExternalAgentFunction:
    """取某 agent 的 sort_order=0 default function（迁移保底 / Phase 1 兼容代理用）。

    不存在时 raise NotFoundError。兼容代理（spec §7.4）必须依赖此函数从
    external_agent_functions 表里挑出默认 function；Phase 1 数据会被 alembic
    迁移成一条 sort_order=0 的 default function（spec §5 / review P1-2）。
    """
    await get_external_agent(agent_id=agent_id, org_id=org_id, db=db)
    result = await db.execute(
        select(ExternalAgentFunction).where(
            ExternalAgentFunction.agent_id == agent_id,
            ExternalAgentFunction.sort_order == 0,
            ExternalAgentFunction.deleted_at.is_(None),
        )
    )
    func = result.scalar_one_or_none()
    if func is None:
        raise NotFoundError(
            "external_agent_function", f"default for agent {agent_id}"
        )
    return func


async def create_function(
    *,
    agent_id: str,
    org_id: str,
    user_id: str,
    name: str,
    summary: str | None,
    invoke_config: dict,
    input_schema: dict | None,
    output_hint: dict | None,
    status: str | None,
    sort_order: int | None,
    db: AsyncSession,
) -> ExternalAgentFunction:
    """新建一个 function。

    - 校验 agent.org_id == org_id（IDOR 防护）
    - 仅 tool 型插件支持 function
    - 校验 invoke_config.endpoint 过 SSRF（org allow-list）
    - invoke_config.auth.token 落库前加密
    - output_hint 拼到 invoke_config._output_hint 子键（review P1-1）
    - sort_order: 若调用方没传，取 max(sort_order)+1（最小 1，0 给迁移默认 function 保留）
    - PluginManifest tool 分支校验整个对象（保证 schema 完整）
    """
    agent = await get_external_agent(agent_id=agent_id, org_id=org_id, db=db)
    if (agent.type or "chat") != "tool":
        raise BadRequestError(
            message="仅 tool 型插件支持 function",
            message_key="errors.external_agent.invalid_function_agent_type",
        )

    # SSRF 闸门（与 create/update agent 端点同款）
    allowed_cidrs = await _load_org_allowed_cidrs(org_id, db)
    from app.services.external_agent_ssrf import validate_invoke_endpoint
    validate_invoke_endpoint(invoke_config["endpoint"], allowed_cidrs)

    # token 加密（与 create_agent 端点同款）
    if invoke_config.get("auth", {}).get("token"):
        invoke_config["auth"]["token"] = encrypt_sensitive(invoke_config["auth"]["token"])
    # output_hint 拼到 _output_hint（review P1-1）
    if output_hint is not None:
        invoke_config["_output_hint"] = output_hint

    # sort_order 兜底：取 max + 1，最小 1（0 给迁移默认 function 保留）
    if sort_order is None:
        max_row = (
            await db.execute(
                select(func.max(ExternalAgentFunction.sort_order)).where(
                    ExternalAgentFunction.agent_id == agent_id,
                    not_deleted(ExternalAgentFunction),
                )
            )
        ).scalar()
        sort_order = max((max_row or 0) + 1, 1)

    # PluginManifest tool 分支校验整体对象（保证 schema 完整）
    from app.schemas.external_agent import PluginManifest
    PluginManifest.model_validate({
        "name": name,
        "type": "tool",
        "invoke": invoke_config,
        "input_schema": input_schema,
    })

    fn = ExternalAgentFunction(
        agent_id=agent_id,
        name=name,
        summary=summary,
        invoke_config=invoke_config,
        input_schema=input_schema,
        status=status or "draft",
        sort_order=sort_order,
        source="manual",
        origin_meta=None,
        version=1,
    )
    db.add(fn)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # (agent_id, name) 或 (agent_id, sort_order) 唯一约束冲突，
        # 前端提示统一用 function_name_conflict；具体冲突原因可查日志。
        raise BadRequestError(
            message="功能名或排序号重复",
            message_key="errors.external_agent.function_name_conflict",
        )
    except Exception:
        await db.rollback()
        raise
    await db.refresh(fn)
    return fn


async def update_function(
    *,
    function_id: str,
    org_id: str,
    updates: dict,
    db: AsyncSession,
) -> ExternalAgentFunction:
    """编辑 function。

    - 校验 org 归属
    - invoke_config / input_schema 变更时 version+1（与 agent.update 同款逻辑）
    - invoke_config.auth.token 重新加密（如果有新值）
    - 重新走 SSRF（如果 endpoint 改了）
    - output_hint 合并到 _output_hint
    """
    fn = await get_function(function_id=function_id, org_id=org_id, db=db)
    if fn.deleted_at is not None:
        raise NotFoundError("external_agent_function", function_id)

    # 浅拷贝 invoke_config，避免原地修改 fn.invoke_config 后比较永远相等
    if "output_hint" in updates and updates["output_hint"] is not None:
        ic = updates.setdefault("invoke_config", dict(fn.invoke_config or {}))
        ic["_output_hint"] = updates.pop("output_hint")

    # 加密新 token（如果有）
    if (
        "invoke_config" in updates
        and updates["invoke_config"].get("auth", {}).get("token")
    ):
        from app.core.security import encrypt_sensitive as _enc
        updates["invoke_config"]["auth"]["token"] = _enc(
            updates["invoke_config"]["auth"]["token"]
        )

    # SSRF 闸门（endpoint 改了或新填）
    if "invoke_config" in updates and "endpoint" in updates["invoke_config"]:
        from app.services.external_agent_ssrf import validate_invoke_endpoint
        await get_external_agent(agent_id=fn.agent_id, org_id=org_id, db=db)
        allowed_cidrs = await _load_org_allowed_cidrs(org_id, db)
        validate_invoke_endpoint(updates["invoke_config"]["endpoint"], allowed_cidrs)

    # version+1 if schema changes
    schema_changed = (
        "invoke_config" in updates
        and updates["invoke_config"] != fn.invoke_config
    ) or (
        "input_schema" in updates
        and updates["input_schema"] != fn.input_schema
    )

    for k, v in updates.items():
        setattr(fn, k, v)
    if schema_changed:
        fn.version = (fn.version or 1) + 1

    await db.commit()
    await db.refresh(fn)
    return fn


async def delete_function(
    *, function_id: str, org_id: str, db: AsyncSession,
) -> None:
    """软删除 function。"""
    fn = await get_function(function_id=function_id, org_id=org_id, db=db)
    fn.soft_delete()
    await db.commit()


async def probe_function(
    *, function_id: str, org_id: str, db: AsyncSession,
) -> dict:
    """function 单功能试调。复用 external_agent_adapter.probe_tool_invoke。"""
    fn = await get_function(function_id=function_id, org_id=org_id, db=db)
    if not fn.invoke_config:
        raise BadRequestError(
            message="function 缺少 invoke_config",
            message_key="errors.external_agent.function_no_invoke_config",
        )

    from app.schemas.external_agent import ManifestInvokeConfig
    from app.services import external_agent_adapter
    from app.services.external_agent_ssrf import validate_invoke_endpoint

    agent = await get_external_agent(agent_id=fn.agent_id, org_id=org_id, db=db)
    allowed_cidrs = await _load_org_allowed_cidrs(org_id, db)
    validate_invoke_endpoint(fn.invoke_config["endpoint"], allowed_cidrs)

    result = await external_agent_adapter.probe_tool_invoke(
        ManifestInvokeConfig.model_validate(fn.invoke_config),
        allowed_cidrs=allowed_cidrs,
    )
    # 写 agent.is_reachable / last_probe（与 probe_agent 端点同款逻辑）
    agent.is_reachable = bool(result.get("ok"))
    agent.last_checked_at = datetime.now(timezone.utc)
    agent.last_probe = {**result, "at": datetime.now(timezone.utc).isoformat()}
    await db.commit()
    await db.refresh(agent)
    return {**result, "reachable": agent.is_reachable}
