"""外部专用 Agent 的 REST API 路由。

创建/更新/同步连接需要 org operator 及以上权限；删除仍需 org admin 权限；
列表查询所有登录成员可见；聊天端点（SSE）仅需普通登录用户。
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks
from app.core.deps import (
    async_session_factory,
    get_current_org,
    get_db,
    require_org_admin,
    require_org_member_role,
)
from app.models.external_agent_chat import ExternalAgentChatSession
from app.models.external_agent_function import ExternalAgentFunction
from app.models.organization import Organization
from app.core.exceptions import AppException, BadRequestError, ForbiddenError, NotFoundError
from app.core.security import encrypt_sensitive
from app.schemas.common import ApiResponse
from app.schemas.external_agent import (
    AttachmentItemWithUrl,
    ChatSessionResponse,
    ExternalAgentCreate,
    ExternalAgentFunctionCreate,
    ExternalAgentFunctionResponse,
    ExternalAgentFunctionUpdate,
    ExternalAgentInvocationResponse,
    ExternalAgentResponse,
    ExternalAgentUpdate,
    MessageResponse,
    PluginConnectivityResult,
    PluginManifest,
    PluginValidateResponse,
)
from app.services import external_agent_adapter, external_agent_service
from app.services import external_agent_chat_service
from app.services import external_agent_invocation_service
from app.services import external_agent_rate_limit
from app.services import openapi_import_service
from app.services.external_agent_ssrf import (
    check_endpoint_allowed,
    validate_invoke_endpoint,
)
from app.services import storage_service
from app.services.external_agent_tool_service import (
    _Unreachable503,
    _ValidationError422,
    get_file_fields,
    has_file_field,
    invoke_tool,
    redact_invoke_config,
    upload_plugin_file,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _persist_messages(
    session_id: str,
    user_content: str,
    user_attachments: list[dict] | None,
    assistant_content: str,
    assistant_thinking: str | None = None,
) -> None:
    """SSE 流结束后异步持久化消息，使用独立 DB Session 避免与请求 Session 竞争。"""
    try:
        async with async_session_factory() as db:
            await external_agent_chat_service.save_messages(
                session_id=session_id,
                user_content=user_content,
                user_attachments=user_attachments,
                assistant_content=assistant_content,
                assistant_thinking=assistant_thinking,
                db=db,
            )
    except Exception as exc:
        logger.warning("Failed to persist chat messages for session %s: %s", session_id, exc)


def _to_response(agent, function_count: int = 0) -> ExternalAgentResponse:
    """将 ORM 对象转换为响应体（capabilities 由 Schema validator 自动解析）。

    list 端点调用时传入每个 agent 的 function 计数（Phase 2 §8.4），
    其它端点（get/create/update）默认 0；前端按 `agent.type === 'tool'`
    决定是否展示。
    """
    payload = {
        c.key: getattr(agent, c.key)
        for c in agent.__table__.columns
    }
    payload["function_count"] = function_count
    return ExternalAgentResponse.model_validate(payload)


async def _get_org_allowed_cidrs(org_id: str, db: AsyncSession) -> list[str]:
    """读取组织当前的 SSRF 白名单（CIDR 列表），每次请求都重新读 DB，不缓存。

    缓存会让管理员调 PUT 后立即生效（即便缓存 1s）也存在窗口期——而 SSRF
    是高频安全相关检查，宁可多一次 DB 查询也不能放过。
    """
    result = await db.execute(
        select(Organization.external_agent_allowed_cidrs).where(Organization.id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return []
    return list(row)


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("", response_model=ApiResponse[ExternalAgentResponse])
async def create_agent(
    body: ExternalAgentCreate,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """创建外部 Agent 连接配置（需要 org operator 及以上）。

    插件化扩展字段（type/invoke_config/input_schema/...）在落库前统一经
    PluginManifest 做一次强制校验，避免保存"形式合法但语义错"的配置。
    """
    user, org = auth

    # 构造 Manifest 校验对象：chat 型不带 invoke 等字段，所以仅以当前已填字段构造
    manifest_body: dict = {
        "name": body.name,
        "type": body.type or "chat",
        "description": body.description,
        "icon_emoji": body.icon_emoji,
        "theme_color": body.theme_color,
    }
    if body.type == "tool":
        manifest_body["invoke"] = body.invoke_config
        manifest_body["input_schema"] = body.input_schema
        manifest_body["output_hint"] = body.output_hint
    else:
        manifest_body["protocol"] = body.protocol
        manifest_body["endpoint"] = body.endpoint
        manifest_body["session_managed_by"] = body.session_managed_by
    try:
        PluginManifest.model_validate(manifest_body)
    except ValidationError as exc:
        raise BadRequestError(
            message="Manifest 校验失败",
            message_key="errors.external_agent.manifest_invalid",
        ) from exc

    # SSRF 闸门（任务 #5）：落库前对有效 endpoint 做私网/回环/云元数据检查。
    # 必须在 Manifest 校验通过后做，否则 schema 错误的请求不会到达这里；
    # 也必须在 encrypt + DB 写入前做，避免无效配置也加密落库。
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)
    effective_endpoint = (
        body.endpoint
        if body.endpoint
        else (body.invoke_config.endpoint if body.invoke_config else None)
    )
    if effective_endpoint:
        validate_invoke_endpoint(effective_endpoint, allowed_cidrs)

    # 加密存储 invoke auth token（与 api_key 走相同机制，避免明文落库）
    invoke_dict = (
        body.invoke_config.model_dump() if body.invoke_config else None
    )
    if invoke_dict and invoke_dict.get("auth", {}).get("token"):
        invoke_dict["auth"]["token"] = encrypt_sensitive(
            invoke_dict["auth"]["token"]
        )
    # output_hint 与 invoke 共享 JSON 列，存为 invoke_config._output_hint 子键
    # （避免单独建一列，与 spec §5 列清单保持一致）
    if invoke_dict is not None and body.output_hint:
        invoke_dict["_output_hint"] = body.output_hint.model_dump()

    agent = await external_agent_service.create_external_agent(
        org_id=org.id,
        name=body.name,
        endpoint=body.endpoint,
        protocol=body.protocol,
        api_key=body.api_key,
        description=body.description,
        capabilities=body.capabilities,
        icon_emoji=body.icon_emoji,
        theme_color=body.theme_color,
        db=db,
        type=body.type,
        session_managed_by=body.session_managed_by,
        invoke_config=invoke_dict,
        input_schema=body.input_schema.model_dump() if body.input_schema else None,
        status=body.status,
    )
    await hooks.emit("operation_audit", action="external_agent.created", target_type="external_agent", target_id=agent.id, actor_id=user.id, org_id=org.id, details={"name": body.name})
    return ApiResponse(data=_to_response(agent))


@router.get("", response_model=ApiResponse[list[ExternalAgentResponse]])
async def list_agents(
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """列出组织内所有外部 Agent（所有登录成员可见）。

    Phase 2 §8.4：tool 型卡片需要展示 function 数（Phase 1 存量插件 = 1，
    导入插件 ≥1）。用 group-by 子查询一次拿全，避免 N+1；chat 型的 count
    留 0（前端按 type 屏蔽）。
    """
    _, org = auth
    agents = await external_agent_service.list_external_agents(org_id=org.id, db=db)
    counts = await _count_functions_by_agent(
        agent_ids=[a.id for a in agents], db=db,
    )
    return ApiResponse(data=[_to_response(a, counts.get(a.id, 0)) for a in agents])


async def _count_functions_by_agent(
    agent_ids: list[str], db: AsyncSession,
) -> dict[str, int]:
    """返回 {agent_id: function_count}（未软删）。

    空 agent_ids 直接返回空 dict；调用方负责用 .get(agent_id, 0) 兜底。
    """
    if not agent_ids:
        return {}
    from sqlalchemy import func
    result = await db.execute(
        select(
            ExternalAgentFunction.agent_id,
            func.count(ExternalAgentFunction.id),
        )
        .where(
            ExternalAgentFunction.agent_id.in_(agent_ids),
            ExternalAgentFunction.deleted_at.is_(None),
        )
        .group_by(ExternalAgentFunction.agent_id)
    )
    return {agent_id: int(count) for agent_id, count in result.all()}


@router.patch("/{agent_id}", response_model=ApiResponse[ExternalAgentResponse])
async def update_agent(
    agent_id: str,
    body: ExternalAgentUpdate,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """更新外部 Agent 配置（需要 org operator 及以上）。

    input_schema/invoke_config 变更时 version 自动 +1（service 层处理），
    落库前同样经 PluginManifest 校验。
    """
    user, org = auth
    updates = body.model_dump(exclude_none=True)

    # 取出当前 agent 状态，与 patch 内容合并用于 Manifest 校验（避免只传
    # input_schema 时漏掉 protocol/endpoint 这种"既要旧又要新"的混合更新）。
    existing = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    effective_type = updates.get("type", existing.type or "chat")
    existing_invoke = existing.invoke_config or {}
    existing_output_hint = existing_invoke.get("_output_hint")

    manifest_body: dict = {
        "name": updates.get("name", existing.name),
        "type": effective_type,
        "description": updates.get("description", existing.description),
        "icon_emoji": updates.get("icon_emoji", existing.icon_emoji),
        "theme_color": updates.get("theme_color", existing.theme_color),
    }
    if effective_type == "tool":
        manifest_body["invoke"] = updates.get("invoke_config", existing_invoke)
        manifest_body["input_schema"] = updates.get("input_schema", existing.input_schema)
        manifest_body["output_hint"] = updates.get("output_hint", existing_output_hint)
    else:
        manifest_body["protocol"] = updates.get("protocol", existing.protocol)
        manifest_body["endpoint"] = updates.get("endpoint", existing.endpoint)
        manifest_body["session_managed_by"] = updates.get(
            "session_managed_by", existing.session_managed_by
        )
    try:
        PluginManifest.model_validate(manifest_body)
    except ValidationError as exc:
        raise BadRequestError(
            message="Manifest 校验失败",
            message_key="errors.external_agent.manifest_invalid",
        ) from exc

    # SSRF 闸门（任务 #5）：更新场景同样在落库前做私网/回环检查；
    # 若 endpoint 字段未传则复用合并后的有效 endpoint（manifest_body 已合并旧值）。
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)
    effective_endpoint = manifest_body.get("endpoint") or manifest_body.get("invoke", {}).get("endpoint")
    if effective_endpoint:
        validate_invoke_endpoint(effective_endpoint, allowed_cidrs)

    # 加密 invoke auth token
    if "invoke_config" in updates and updates["invoke_config"].get("auth", {}).get("token"):
        updates["invoke_config"]["auth"]["token"] = encrypt_sensitive(
            updates["invoke_config"]["auth"]["token"]
        )
    # output_hint 合并到 invoke_config._output_hint 子键（与 create 路径一致）
    if "output_hint" in updates:
        ic = updates.setdefault("invoke_config", dict(existing.invoke_config or {}))
        if updates["output_hint"] is None:
            ic.pop("_output_hint", None)
        else:
            ic["_output_hint"] = updates.pop("output_hint")

    agent = await external_agent_service.update_external_agent(
        agent_id=agent_id, org_id=org.id, updates=updates, db=db
    )
    await hooks.emit("operation_audit", action="external_agent.updated", target_type="external_agent", target_id=agent_id, actor_id=user.id, org_id=org.id)
    return ApiResponse(data=_to_response(agent))


@router.post("/{agent_id}/probe", response_model=ApiResponse[dict])
async def probe_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """手动健康探测（需要 org operator 及以上）：按 agent.type 调用对应探活路径，
    写回 is_reachable + last_probe，结果原样返回供前端展示。"""
    user, org = auth
    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    api_key = external_agent_service.get_decrypted_api_key(agent)
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)
    if (agent.type or "chat") == "tool":
        from app.schemas.external_agent import ManifestInvokeConfig
        result = await external_agent_adapter.probe_tool_invoke(
            ManifestInvokeConfig.model_validate(agent.invoke_config or {"endpoint": ""}),
            allowed_cidrs=allowed_cidrs,
        )
    else:
        result = await external_agent_adapter.probe_chat_endpoint(
            endpoint=agent.endpoint, api_key=api_key, protocol=agent.protocol,
            allowed_cidrs=allowed_cidrs,
        )

    agent.is_reachable = bool(result.get("ok"))
    agent.last_checked_at = datetime.now(timezone.utc)
    agent.last_probe = {**result, "at": datetime.now(timezone.utc).isoformat()}
    await db.commit()
    await db.refresh(agent)

    await hooks.emit(
        "operation_audit", action="external_agent.probed",
        target_type="external_agent", target_id=agent_id,
        actor_id=user.id, org_id=org.id,
        details={"reachable": agent.is_reachable},
    )
    return ApiResponse(data={"reachable": agent.is_reachable, **result})


@router.delete("/{agent_id}", response_model=ApiResponse[None])
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_admin),
):
    """软删除外部 Agent（需要 org admin）。"""
    user, org = auth
    await external_agent_service.delete_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    await hooks.emit("operation_audit", action="external_agent.deleted", target_type="external_agent", target_id=agent_id, actor_id=user.id, org_id=org.id)
    return ApiResponse(data=None)


# ── Function CRUD（spec §7.2）───────────────────────────────────────────────


@router.get("/{agent_id}/functions", response_model=ApiResponse[list[ExternalAgentFunctionResponse]])
async def list_functions(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """列出某插件的所有 function（按 sort_order 升序，未软删）。

    权限：所有组织成员可见（用户端表单页需要先拉功能列表才能渲染），
    管理操作（POST/PATCH/DELETE/probe）仍走 operator 校验。
    """
    _, org = auth
    funcs = await external_agent_service.list_functions(
        agent_id=agent_id, org_id=org.id, db=db,
    )
    return ApiResponse(
        data=[ExternalAgentFunctionResponse.model_validate(f) for f in funcs]
    )


@router.post("/{agent_id}/functions", response_model=ApiResponse[ExternalAgentFunctionResponse])
async def create_function(
    agent_id: str,
    body: ExternalAgentFunctionCreate,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """手动加功能（需要 org operator 及以上）。

    校验由 service 层完成：agent 类型必须是 tool、SSRF 闸门、token 加密、
    output_hint 合并到 invoke_config._output_hint、PluginManifest tool 分支校验。
    """
    user, org = auth
    invoke_dict = body.invoke_config.model_dump() if body.invoke_config else None
    if invoke_dict is None:
        # Manifest 校验要求 tool 型必须提供 invoke_config；API 层在 service
        # 之前兜一道，避免抛 Pydantic 字段缺失错误混入 SSRF 错误码。
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            message="function 必须提供 invoke_config",
            message_key="errors.external_agent.manifest_invalid",
        )

    func = await external_agent_service.create_function(
        agent_id=agent_id,
        org_id=org.id,
        user_id=str(user.id),
        name=body.name,
        summary=body.summary,
        invoke_config=invoke_dict,
        input_schema=body.input_schema.model_dump() if body.input_schema else None,
        output_hint=body.output_hint.model_dump() if body.output_hint else None,
        status=body.status,
        sort_order=body.sort_order,
        db=db,
    )
    await hooks.emit(
        "operation_audit",
        action="external_agent.function_created",
        target_type="external_agent_function",
        target_id=func.id,
        actor_id=user.id,
        org_id=org.id,
        details={"agent_id": agent_id, "name": body.name},
    )
    return ApiResponse(data=ExternalAgentFunctionResponse.model_validate(func))


@router.patch(
    "/{agent_id}/functions/{function_id}",
    response_model=ApiResponse[ExternalAgentFunctionResponse],
)
async def update_function(
    agent_id: str,
    function_id: str,
    body: ExternalAgentFunctionUpdate,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """编辑功能（需要 org operator 及以上）。"""
    user, org = auth
    updates = body.model_dump(exclude_none=True)
    if "invoke_config" in updates and updates["invoke_config"] is not None:
        # pydantic v2 行为：BaseModel 子类用 model_dump 已是 dict
        pass

    func = await external_agent_service.update_function(
        function_id=function_id, org_id=org.id, updates=updates, db=db,
    )
    await hooks.emit(
        "operation_audit",
        action="external_agent.function_updated",
        target_type="external_agent_function",
        target_id=function_id,
        actor_id=user.id,
        org_id=org.id,
        details={"agent_id": agent_id},
    )
    return ApiResponse(data=ExternalAgentFunctionResponse.model_validate(func))


@router.delete(
    "/{agent_id}/functions/{function_id}",
    response_model=ApiResponse[None],
)
async def delete_function(
    agent_id: str,
    function_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_admin),
):
    """软删除功能（需要 org admin，与 Phase 1 delete_agent 一致）。"""
    user, org = auth
    await external_agent_service.delete_function(
        function_id=function_id, org_id=org.id, db=db,
    )
    await hooks.emit(
        "operation_audit",
        action="external_agent.function_deleted",
        target_type="external_agent_function",
        target_id=function_id,
        actor_id=user.id,
        org_id=org.id,
        details={"agent_id": agent_id},
    )
    return ApiResponse(data=None)


@router.post(
    "/{agent_id}/functions/{function_id}/probe",
    response_model=ApiResponse[dict],
)
async def probe_function(
    agent_id: str,
    function_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """单功能试调（复用 probe_tool_invoke，写回 agent.is_reachable + last_probe）。

    限流（spec §9.5 + review H-1）：与 agent /invoke 端点对齐，先于 IDOR/状态
    检查；调用方按 (agent_id, user_id) 桶计费，命中 429 直接拒绝。
    """
    user, org = auth
    await external_agent_rate_limit.check_rate_limit(
        agent_id=agent_id, user_id=str(user.id),
    )

    result = await external_agent_service.probe_function(
        function_id=function_id, org_id=org.id, db=db,
    )
    await hooks.emit(
        "operation_audit",
        action="external_agent.function_probed",
        target_type="external_agent_function",
        target_id=function_id,
        actor_id=user.id,
        org_id=org.id,
        details={"agent_id": agent_id, "reachable": result.get("reachable")},
    )
    return ApiResponse(data=result)


# ── function 级 form/invoke/files（spec §7.3）──────────────────────────────


@router.get(
    "/{agent_id}/functions/{function_id}/form",
    response_model=ApiResponse[dict],
)
async def get_function_form(
    agent_id: str,
    function_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """返回该 function 的 input_schema（含 description/default）+ 脱敏 invoke_config。

    校验：
    - function 归属 agent（URL 错配 → 404）
    - function.status == active → 否则 403 errors.external_agent.function_not_active
    - agent.status == active → 否则 403 errors.external_agent.not_active

    invoke_config.auth.token 自动脱敏（同 Phase 1 GET /{id}/form），output_hint
    从 invoke_config._output_hint 子键抽出（review P1-1）。
    """
    _, org = auth
    func = await external_agent_service.get_function(
        function_id=function_id, org_id=org.id, db=db
    )
    if func.agent_id != agent_id:
        raise NotFoundError("external_agent_function", function_id)
    if func.status != "active":
        raise ForbiddenError(
            message="功能未启用",
            message_key="errors.external_agent.function_not_active",
        )

    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    if agent.status != "active":
        raise ForbiddenError(
            message="插件未启用",
            message_key="errors.external_agent.not_active",
        )

    invoke_config = func.invoke_config or {}
    output_hint = (
        invoke_config.pop("_output_hint", None)
        if isinstance(invoke_config, dict) else None
    )

    return ApiResponse(data={
        "function_id": func.id,
        "agent_id": agent.id,
        "name": func.name,
        "summary": func.summary,
        "version": func.version,
        "input_schema": func.input_schema,
        "output_hint": output_hint,
        "invoke_config": redact_invoke_config(invoke_config),
    })


@router.post(
    "/{agent_id}/functions/{function_id}/invoke",
    response_model=ApiResponse[dict],
)
async def invoke_function(
    agent_id: str,
    function_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """function 级 invoke：复用 invoke_tool，数据来源改为 function.input_schema/invoke_config。

    流程（spec §9.5 + review H-1）：
      1. 限流先于鉴权 / status（防拒绝服务向量）
      2. function 归属 + status=active + agent.status=active + is_reachable
      3. 解析请求体 + 校验 submit_params
      4. 调 invoke_tool 传 function_invoke_config / function_input_schema

    422 / 503 错误的捕获与 Phase 1 /invoke 路径对齐；上游非 2xx 包 success:false。
    """
    user, org = auth

    # 限流（spec §9.5 + review H-1）：先于鉴权 / status，与 /invoke 行为一致
    await external_agent_rate_limit.check_rate_limit(
        agent_id=agent_id, user_id=str(user.id),
    )

    func = await external_agent_service.get_function(
        function_id=function_id, org_id=org.id, db=db
    )
    if func.agent_id != agent_id:
        raise NotFoundError("external_agent_function", function_id)
    if func.status != "active":
        raise ForbiddenError(
            message="功能未启用",
            message_key="errors.external_agent.function_not_active",
        )

    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    if agent.status != "active":
        raise ForbiddenError(
            message="插件未启用",
            message_key="errors.external_agent.not_active",
        )
    if not agent.is_reachable:
        raise HTTPException(
            status_code=503,
            detail={
                "code": 50300,
                "error_code": 50300,
                "message_key": "errors.external_agent.invoke_unreachable",
                "message": "外部服务暂不可达",
            },
        )

    try:
        body = await request.json()
    except Exception:
        raise BadRequestError(
            message="请求体必须为 JSON",
            message_key="errors.external_agent.invoke_validation_error",
        )
    submit_params = body.get("params") if isinstance(body, dict) else None
    if not isinstance(submit_params, dict):
        raise BadRequestError(
            message="提交参数格式不正确",
            message_key="errors.external_agent.invoke_validation_error",
        )

    try:
        result = await invoke_tool(
            agent=agent,
            org_id=org.id,
            user_id=str(user.id),
            submit_params=submit_params,
            db=db,
            allowed_cidrs=await _get_org_allowed_cidrs(org.id, db),
            function_invoke_config=func.invoke_config,
            function_input_schema=func.input_schema,
        )
    except _ValidationError422 as exc:
        raise AppException(
            code=42200,
            error_code=42200,
            message="提交参数不合法",
            message_key="errors.external_agent.invoke_validation_error",
            status_code=422,
            extra={"field_errors": exc.field_errors},
        )
    except _Unreachable503 as exc:
        # 不可达也是一次真实发生的调用尝试，落历史（success=false）供用户回看
        await external_agent_invocation_service.record_invocation(
            agent_id=agent_id, org_id=org.id, user_id=str(user.id),
            function_id=func.id, function_name=func.name,
            params=submit_params,
            result={"success": False, "error": exc.message},
            db=db,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": 50300,
                "error_code": 50300,
                "message_key": exc.message_key,
                "message": exc.message,
            },
        )

    # 成功 / 上游非 2xx（success:false，HTTP 200）都落一条调用历史；
    # 422 参数校验失败未发起外部调用，不记录。
    await external_agent_invocation_service.record_invocation(
        agent_id=agent_id, org_id=org.id, user_id=str(user.id),
        function_id=func.id, function_name=func.name,
        params=submit_params, result=result, db=db,
    )

    if result.get("success") is False:
        return ApiResponse(data={
            **result,
            "message_key": "errors.external_agent.invoke_upstream_error",
        })
    return ApiResponse(data=result)


@router.post(
    "/{agent_id}/functions/{function_id}/files",
    response_model=ApiResponse[dict],
)
async def upload_function_file(
    agent_id: str,
    function_id: str,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """function 级文件上传：白名单 / max_mb 读 function.input_schema 里的 file 字段。

    校验：
    - function 归属 agent → 否则 404
    - function.status == active → 否则 403 function_not_active
    - function.input_schema 含 file 字段 → 否则 400 file_upload_not_supported

    复用 upload_plugin_file；agent 对象仍传入（用于审计钩子），max_mb 从
    file 字段定义读（缺省走 20MB）。
    """
    _, org = auth
    func = await external_agent_service.get_function(
        function_id=function_id, org_id=org.id, db=db
    )
    if func.agent_id != agent_id:
        raise NotFoundError("external_agent_function", function_id)
    if func.status != "active":
        raise ForbiddenError(
            message="功能未启用",
            message_key="errors.external_agent.function_not_active",
        )

    # 解析 function.input_schema 找第一个 file 字段的白名单 + max_mb
    file_fields = get_file_fields(func.input_schema or {})
    if not file_fields:
        raise BadRequestError(
            message="该功能不支持文件上传",
            message_key="errors.external_agent.file_upload_not_supported",
        )
    _, fdef = file_fields[0]
    fdef_dict = fdef if isinstance(fdef, dict) else (
        fdef.model_dump() if hasattr(fdef, "model_dump") else {}
    )
    field_accept = fdef_dict.get("accept")
    field_max_mb = fdef_dict.get("max_mb")

    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    content = await file.read()
    result = await upload_plugin_file(
        file_content=content,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        agent=agent,
        field_accept=field_accept,
        max_mb=field_max_mb,
        org_id=org.id,
    )
    return ApiResponse(data=result)


# ── 调用历史（tool 型插件，用户侧，spec §6.2 延伸）──────────────────────────


@router.get(
    "/{agent_id}/invocations",
    response_model=ApiResponse[list[ExternalAgentInvocationResponse]],
)
async def list_my_invocations(
    agent_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """列出当前用户在指定插件下的调用历史（普通用户可见自己的）。

    返回最近 N 条调用记录（按 created_at 倒序），包含：
    - function_name / params_summary / success / upstream_status / latency_ms / created_at
    - result_data：完整 invoke 响应（50KB 截断），点击历史项可重放完整结果

    安全：强制 (agent_id, org_id, user_id) 过滤——agent 归属组织校验（跨 org 404），
    记录按 user_id 过滤（用户只能看到自己的，无跨用户泄露面）。
    仅 tool 型插件会有记录（invoke 端点写入）；chat 型 agent 恒返回空列表。
    """
    user, org = auth
    await external_agent_service.get_external_agent(agent_id=agent_id, org_id=org.id, db=db)
    rows = await external_agent_invocation_service.list_invocations(
        agent_id=agent_id,
        org_id=org.id,
        user_id=str(user.id),
        limit=limit,
        db=db,
    )
    return ApiResponse(
        data=[ExternalAgentInvocationResponse.model_validate(r) for r in rows]
    )


# ── OpenAPI 导入（spec §7.1 / Task 6）───────────────────────────────────────────


# 上限常量：5MB（spec §9.4）。preview/confirm 共享。
MAX_OPENAPI_DOC_SIZE = 5 * 1024 * 1024


def _extract_openapi_servers(doc: dict) -> list[dict]:
    """从 OpenAPI 3.x / Swagger 2.0 dict 抽取 servers 列表。

    3.x: 读取 doc.servers（list of {url, description?, variables?}）
    2.0:  合成 [{url: "{schemes[0]}://{host}{basePath}"}]（缺字段时合成空 url）

    返回 list 形态，便于下游统一校验 endpoint 域（review Z-2）。
    """
    if "openapi" in doc:
        servers = doc.get("servers") or []
        if isinstance(servers, list) and servers:
            return [s for s in servers if isinstance(s, dict)]
        return [{"url": ""}]
    if "swagger" in doc:
        schemes = doc.get("schemes") or ["https"]
        scheme = schemes[0] if isinstance(schemes, list) and schemes else "https"
        host = doc.get("host") or ""
        base_path = doc.get("basePath") or ""
        if not isinstance(host, str):
            host = ""
        if not isinstance(base_path, str):
            base_path = ""
        url = f"{scheme}://{host}{base_path}"
        return [{"url": url}]
    return []


def _extract_spec_version(doc: dict) -> str:
    """从 doc 取 raw `openapi` / `swagger` 字段原文（review P2-7：不映射）。"""
    return str(doc.get("openapi") or doc.get("swagger") or "unknown")


def _draft_to_invoke_config(
    draft,
    base_url: str,
    auth_config: dict,
    has_file_field_flag: bool,
) -> dict:
    """从 parser draft + auth 配置 + base_url 构造 invoke_config dict（ManifestInvokeConfig 形态）。

    - `endpoint` = base_url.rstrip("/") + draft.path（base_url 为空时仅 path，会被 SSRF 闸门拦截）
    - `auth`：从 auth_config 取（type / header_name / token）
    - `timeout_seconds` 固定 30s
    - `pass_mode`：含 file 字段时 multipart（文件流式转发），否则 url_ref（普通字段
      以 JSON body 发送）——绝大多数 REST/JSON 接口（如 RAG /query）收 JSON，
      multipart 表单会被 422 拒绝；multipart 只在有文件时才有必要。
    """
    base = (base_url or "").rstrip("/")
    full_endpoint = (base + draft.path) if base else draft.path
    auth_type = auth_config.get("type", "none")
    if auth_type not in ("none", "bearer", "api_key_header"):
        auth_type = "none"
    return {
        "endpoint": full_endpoint,
        "method": draft.method,
        "auth": {
            "type": auth_type,
            "header_name": auth_config.get("header_name"),
            "token": auth_config.get("token"),
        },
        "timeout_seconds": 30,
        "pass_mode": "multipart" if has_file_field_flag else "url_ref",
    }


@router.post("/plugins/import/openapi/preview", response_model=ApiResponse[dict])
async def import_openapi_preview(
    body: dict,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """OpenAPI 导入预览：拉取文档 + 解析 → 返回草稿列表（不落库）。

    请求体：{ "doc_url": "http://..." } 或 { "doc": {...} }（二选一）。

    响应：
      {
        "spec_version": "3.0.0" or "2.0"（raw 字段原值，review P2-7）,
        "servers": [...原始 servers 列表...],
        "functions": [
          {
            "name", "summary", "method", "path",
            "fields": [...FieldDraft...],
            "output_hint_suggestion": {...},
            "warnings": [...]
        }],
        "warnings": [...]  // 全局警告（如 servers URL 异常）
      }

    不落库，不发起 doc 解析以外的网络请求（doc_url 必过 SSRF + 5MB + 15s + JSON 解析）。
    """
    user, org = auth
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)

    # 1. 拉取/取 doc
    doc: dict | None = None
    if "doc_url" in body and body["doc_url"] is not None:
        doc_url = body["doc_url"]
        # fetch_openapi_doc 自身会处理 SSRF/timeout/5MB/JSON 解析并抛 BadRequestError
        doc = await openapi_import_service.fetch_openapi_doc(
            doc_url, allowed_cidrs=allowed_cidrs,
        )
    elif "doc" in body and body["doc"] is not None:
        doc = body["doc"]
        if not isinstance(doc, dict):
            raise BadRequestError(
                "doc 必须为对象",
                "errors.external_agent.openapi_invalid_json",
            )
    else:
        raise BadRequestError(
            "请求体必须包含 doc_url 或 doc",
            "errors.external_agent.openapi_missing_source",
        )

    # 2. 解析
    try:
        drafts = await openapi_import_service.parse_openapi_doc(doc)
    except (ValueError, Exception) as exc:
        raise BadRequestError(
            f"OpenAPI 解析失败：{exc}",
            "errors.external_agent.openapi_parse_failed",
        ) from exc

    # 3. endpoint 域校验（review Z-2：解析出的 endpoint host 必须落在 doc.servers 声明域内）
    servers = _extract_openapi_servers(doc)
    spec_version = _extract_spec_version(doc)
    endpoint_warnings = openapi_import_service.validate_parsed_endpoints_in_servers(
        drafts, servers,
    )
    for draft, ws in zip(drafts, endpoint_warnings):
        if ws:
            draft.warnings.append(ws)

    return ApiResponse(data={
        "spec_version": spec_version,
        "servers": servers,
        "functions": [d.model_dump() for d in drafts],
        "warnings": [],  # 全局 warnings 占位；当前 parser 已把全局警告写入每个 draft
    })


@router.post("/plugins/import/openapi", response_model=ApiResponse[dict])
async def import_openapi_confirm(
    body: dict,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """OpenAPI 导入确认：服务端重新解析（不信前端）+ 创建 agent + 批量创建 functions（spec §7.1 / §9.5）。

    请求体：
      {
        "name": "插件名", "description"?, "icon_emoji"?, "theme_color"?,
        "doc_url"?, "doc"?,
        "auth": { "type": "bearer"|"api_key_header"|"none", "header_name"?, "token"? },
        "selected": [ { "name", "summary"?, "method", "path",
                        "output_hint"?, "field_overrides"? : {字段名: {default?, description?, ui?}}} ]
      }

    行为：
      1. 重新拉取/解析（服务端为准；spec §9.5）
      2. 按 selected.method+path 匹配草稿
      3. 应用 field_overrides（用户调整）
      4. 创建 agent（type=tool, status=draft, invoke_config.auth 为统一鉴权）
      5. 批量创建 functions（source=openapi_import, status=draft, origin_meta={path, method, spec_version}）
      6. emit operation_audit
    """
    user, org = auth
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)

    # 1. 服务端重新解析（spec §9.5：不信任前端回传的草稿）
    doc: dict | None = None
    if "doc_url" in body and body["doc_url"] is not None:
        doc = await openapi_import_service.fetch_openapi_doc(
            body["doc_url"], allowed_cidrs=allowed_cidrs,
        )
    elif "doc" in body and body["doc"] is not None:
        doc = body["doc"]
        if not isinstance(doc, dict):
            raise BadRequestError(
                "doc 必须为对象",
                "errors.external_agent.openapi_invalid_json",
            )
    else:
        raise BadRequestError(
            "请求体必须包含 doc_url 或 doc",
            "errors.external_agent.openapi_missing_source",
        )

    try:
        drafts = await openapi_import_service.parse_openapi_doc(doc)
    except (ValueError, Exception) as exc:
        raise BadRequestError(
            f"OpenAPI 解析失败：{exc}",
            "errors.external_agent.openapi_parse_failed",
        ) from exc

    spec_version = _extract_spec_version(doc)
    servers = _extract_openapi_servers(doc)

    # 2. 按 (method, path) 匹配草稿
    draft_map: dict[tuple[str, str], object] = {
        (d.method.upper(), d.path): d for d in drafts
    }
    selected = body.get("selected") or []
    if not isinstance(selected, list) or not selected:
        raise BadRequestError(
            "selected 必须为非空数组",
            "errors.external_agent.openapi_no_selection",
        )

    # 3. auth 校验 + 构造
    auth_config = body.get("auth") or {}
    if not isinstance(auth_config, dict):
        auth_config = {}
    auth_type = auth_config.get("type", "none")
    if auth_type not in ("none", "bearer", "api_key_header"):
        raise BadRequestError(
            "鉴权配置不合法",
            "errors.external_agent.openapi_invalid_auth",
        )

    # 4. base_url：从 doc.servers 取第一个；swagger 2.0 用合成 URL
    base_url = ""
    for srv in servers:
        u = srv.get("url") if isinstance(srv, dict) else None
        if isinstance(u, str) and u and not u.startswith("/"):
            base_url = u
            break
        if isinstance(u, str) and u.startswith("/") and base_url == "":
            # 相对路径 + 没有 override 时不强制覆盖，留空让 endpoint 只剩 path
            base_url = ""
    # Swagger 2.0 的合成 URL 已是完整 http(s) 形式，会被上面的过滤捕获；
    # 若没有合法 server URL（仅相对路径），base_url 留空。

    # 5. 目标 agent：body 带 agent_id = 追加模式（管理页再导入，不新建插件）；
    #    否则新建（type=tool, status=draft）。
    existing_names: set[str] = set()
    existing_ops: set[tuple[str, str]] = set()
    next_sort = 1
    append_agent_id = body.get("agent_id")
    if append_agent_id:
        agent = await external_agent_service.get_external_agent(
            agent_id=str(append_agent_id), org_id=org.id, db=db,
        )
        if (agent.type or "chat") != "tool":
            raise BadRequestError(
                "仅工具型插件支持追加功能",
                "errors.external_agent.openapi_append_not_tool",
            )
        fn_rows = (await db.execute(
            select(
                ExternalAgentFunction.name,
                ExternalAgentFunction.sort_order,
                ExternalAgentFunction.origin_meta,
            ).where(
                ExternalAgentFunction.agent_id == agent.id,
                ExternalAgentFunction.deleted_at.is_(None),
            )
        )).all()
        existing_names = {r[0] for r in fn_rows}
        next_sort = max((r[1] for r in fn_rows), default=0) + 1
        # 追加去重：同 (method, path) 的接口已存在时跳过，不重复建功能
        for r in fn_rows:
            origin = r[2] if isinstance(r[2], dict) else None
            if origin:
                m = str(origin.get("method") or "").upper()
                p = str(origin.get("path") or "")
                if m and p:
                    existing_ops.add((m, p))
    else:
        first_sel = selected[0]
        if not isinstance(first_sel, dict):
            raise BadRequestError(
                "selected 项必须为对象",
                "errors.external_agent.openapi_selected_not_found",
            )
        first_method = str(first_sel.get("method", "")).upper()
        first_path = str(first_sel.get("path", ""))
        first_draft = draft_map.get((first_method, first_path))
        if first_draft is None:
            raise BadRequestError(
                f"selected[0] (method={first_method}, path={first_path}) 在文档中找不到",
                "errors.external_agent.openapi_selected_not_found",
            )

        # 第一个 function 的 invoke_config + agent-level invoke_config.endpoint（兼容代理用）
        first_has_file = any(f.type == "file" for f in first_draft.fields)
        first_invoke_config = _draft_to_invoke_config(
            first_draft, base_url, auth_config, first_has_file,
        )
        # 把 _output_hint 拼到 invoke_config（与 create_function 同款）
        if first_sel.get("output_hint"):
            first_invoke_config["_output_hint"] = first_sel["output_hint"]

        # 6. 创建 agent（type=tool, status=draft）
        # 旧列 endpoint 必须 NOT NULL → 取第一个 function 的 endpoint 兜底
        first_endpoint = first_invoke_config.get("endpoint") or first_path
        # token 由 service 层 encrypt_sensitive 落库（不预先加密；create_external_agent 会做）
        agent = await external_agent_service.create_external_agent(
            org_id=org.id,
            name=body["name"],
            endpoint=first_endpoint,
            protocol="openai_compatible",  # tool 型不影响，但列 NOT NULL
            api_key=auth_config.get("token"),
            description=body.get("description"),
            capabilities=[],
            icon_emoji=body.get("icon_emoji"),
            theme_color=body.get("theme_color"),
            db=db,
            type="tool",
            session_managed_by=None,
            invoke_config=first_invoke_config,
            input_schema=None,
            status="draft",
        )

    # 7. 批量创建 functions（跳过 doc 中找不到的项 + 追加模式下已存在的同接口）
    created_functions: list = []
    skipped_existing: list[dict] = []
    for idx, sel in enumerate(selected):
        if not isinstance(sel, dict):
            continue
        method = str(sel.get("method", "")).upper()
        path = str(sel.get("path", ""))
        draft = draft_map.get((method, path))
        if draft is None:
            logger.warning(
                "OpenAPI 导入跳过未匹配的 selected 项: method=%s path=%s", method, path,
            )
            continue
        if (method, path) in existing_ops:
            # 去重：该接口已作为功能存在（按 origin_meta 的 method+path 判定）
            skipped_existing.append({
                "name": str(sel.get("name") or draft.name),
                "method": method,
                "path": path,
            })
            continue

        # 应用 field_overrides（就地修改 draft 副本，不影响 draft_map）
        overrides = sel.get("field_overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        # 先深拷贝 draft.fields，避免污染其他 function
        from copy import deepcopy
        new_fields = [deepcopy(f) for f in draft.fields]
        field_dict = {f.name: f for f in new_fields}
        for fname, override in overrides.items():
            target = field_dict.get(fname)
            if target is None or not isinstance(override, dict):
                continue
            # label 覆盖：给无中文 description 的字段补中文名（用户端表单显示用）
            if isinstance(override.get("label"), str) and override["label"].strip():
                target.label = override["label"].strip()
            if "default" in override:
                target.default = override["default"]
            if "description" in override:
                target.description = override["description"]
            if "ui" in override:
                target.ui = override["ui"]

        has_file = any(f.type == "file" for f in new_fields)
        invoke_config = _draft_to_invoke_config(
            draft, base_url, auth_config, has_file,
        )
        if sel.get("output_hint"):
            invoke_config["_output_hint"] = sel["output_hint"]

        input_schema_dict = {
            "order": [f.name for f in new_fields],
            "fields": {f.name: f.model_dump() for f in new_fields},
        }

        # 功能名同插件内唯一：与已有（含本轮先前创建）冲突时自动加 _2/_3 后缀
        # （评审 P2-2：解析层不管唯一性，API 层兜底）
        base_name = str(sel.get("name") or draft.name or f"fn_{next_sort}")
        final_name = base_name
        suffix_n = 2
        while final_name in existing_names:
            final_name = f"{base_name}_{suffix_n}"
            suffix_n += 1
        existing_names.add(final_name)

        func = await external_agent_service.create_function(
            agent_id=agent.id,
            org_id=org.id,
            user_id=str(user.id),
            name=final_name,
            summary=sel.get("summary") or draft.summary,
            invoke_config=invoke_config,
            input_schema=input_schema_dict,
            output_hint=None,  # 已在 invoke_config._output_hint 上
            status="draft",
            sort_order=next_sort,  # 追加模式从 max+1 续排；新建模式从 1 起
            db=db,
        )
        next_sort += 1
        # 设置 source / origin_meta（service.create_function 默认 source='manual'）
        func.source = "openapi_import"
        func.origin_meta = {
            "path": draft.path,
            "method": draft.method,
            "operationId": draft.name,  # parser 已清洗为合法 identifier
            "spec_version": spec_version,
        }
        created_functions.append(func)

    await db.commit()
    # commit 后 ORM 默认 expire 所有属性；显式 refresh 让后续 model_validate
    # 提取 updated_at / created_at 等 lazy 字段不抛 MissingGreenlet。
    for func in created_functions:
        await db.refresh(func)
    await db.refresh(agent)

    await hooks.emit(
        "operation_audit",
        action="external_agent.imported_from_openapi",
        target_type="external_agent",
        target_id=agent.id,
        actor_id=user.id,
        org_id=org.id,
        details={
            "name": body.get("name") or agent.name,
            "append": bool(append_agent_id),
            "function_count": len(created_functions),
        },
    )

    return ApiResponse(data={
        "agent": _to_response(agent),
        "functions": [
            ExternalAgentFunctionResponse.model_validate(f).model_dump()
            for f in created_functions
        ],
        # 追加去重：这些 selected 接口已存在为功能，未重复创建
        "skipped_existing": skipped_existing,
    })


# ── Manifest 校验（管理端向导第 3 步）───────────────────────────────────────────

@router.post("/plugins/validate", response_model=ApiResponse[PluginValidateResponse])
async def validate_plugin_manifest(
    body: dict,
    auth=Depends(require_org_member_role("operator")),
    db: AsyncSession = Depends(get_db),
):
    """校验插件 Manifest 合法性 + 连通性试调，只校验不落库（需 org operator 及以上）。

    schema 校验失败时直接返回 schema_ok=false + 错误列表，不发起任何外部请求；
    schema 通过后按 type 分支做一次连通性试调（chat 型探活路径、tool 型 GET 直调
    /非 GET 仅 DNS/TCP 级探测），外部服务不可达只作为 warning 提示，不影响
    schema_ok（不可达时仍允许保存为草稿，见方案 §6.1）。

    SSRF 闸门（任务 #5）：在 schema 通过后、连通性试调前先检查目标 endpoint。
    命中 SSRF 拦截时，**不发起任何外网请求**，把错误作为 schema_error 返回，
    避免向导第 3 步意外打到内网元数据服务。
    """
    try:
        manifest = PluginManifest.model_validate(body)
    except ValidationError as exc:
        return ApiResponse(data=PluginValidateResponse(
            schema_ok=False,
            schema_errors=[e["msg"] for e in exc.errors()],
        ))

    # SSRF 闸门：先校验每个 endpoint，未通过则把错误塞进 schema_errors 并 return，
    # 让向导明确提示管理员"目标地址不允许"，且不会触发任何外网请求。
    _, org = auth
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)
    ssrf_errors: list[str] = []
    if manifest.type == "chat" and manifest.endpoint:
        ok, err = check_endpoint_allowed(manifest.endpoint, allowed_cidrs)
        if not ok:
            ssrf_errors.append(f"chat endpoint: {err}")
    elif manifest.type == "tool" and manifest.invoke is not None:
        ok, err = check_endpoint_allowed(manifest.invoke.endpoint, allowed_cidrs)
        if not ok:
            ssrf_errors.append(f"invoke.endpoint: {err}")
    if ssrf_errors:
        return ApiResponse(data=PluginValidateResponse(
            schema_ok=False,
            schema_errors=ssrf_errors,
        ))

    warnings: list[str] = []
    if manifest.type == "chat":
        raw = await external_agent_adapter.probe_chat_endpoint(
            endpoint=manifest.endpoint, api_key=manifest.api_key, protocol=manifest.protocol,
            allowed_cidrs=allowed_cidrs,
        )
    else:
        raw = await external_agent_adapter.probe_tool_invoke(
            manifest.invoke, allowed_cidrs=allowed_cidrs,
        )
        if raw.get("skipped_invoke"):
            warnings.append("必填参数缺少默认值，试调仅做网络可达性检查，未实际调用接口")

    connectivity = PluginConnectivityResult.model_validate(raw)
    if not connectivity.ok:
        warnings.append("外部服务当前不可达，可保存为草稿但无法启用")

    return ApiResponse(data=PluginValidateResponse(
        schema_ok=True,
        connectivity=connectivity,
        warnings=warnings,
    ))


# ── Sync（连接验证）────────────────────────────────────────────────────────────

@router.post("/{agent_id}/sync", response_model=ApiResponse[dict])
async def sync_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(require_org_member_role("operator")),
):
    """验证外部 Agent 连接可达性，更新 is_reachable（需要 org operator 及以上）。

    NAP 协议额外调用 /meta，将 capabilities / description 同步回数据库。
    """
    user, org = auth
    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    api_key = external_agent_service.get_decrypted_api_key(agent)
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)

    if (agent.type or "chat") == "tool":
        # tool 型：protocol 是占位符（openai_compatible），不能用 verify_connection
        # 去 GET {endpoint}/v1/models（对 REST 接口必然 404 误判不可达）。
        # 改用与 /probe 一致的 probe_tool_invoke（按 invoke_config 的 method 探测，
        # 405/404 视为可达），并同样写 last_probe 供前端展示。
        from app.schemas.external_agent import ManifestInvokeConfig
        result = await external_agent_adapter.probe_tool_invoke(
            ManifestInvokeConfig.model_validate(agent.invoke_config or {"endpoint": ""}),
            allowed_cidrs=allowed_cidrs,
        )
        reachable = bool(result.get("ok"))
        agent.is_reachable = reachable
        agent.last_checked_at = datetime.now(timezone.utc)
        agent.last_probe = {**result, "at": datetime.now(timezone.utc).isoformat()}
        await db.commit()
        await hooks.emit(
            "operation_audit", action="external_agent.synced",
            target_type="external_agent", target_id=agent_id,
            actor_id=user.id, org_id=org.id, details={"reachable": reachable},
        )
        return ApiResponse(data={"reachable": reachable, "agent_id": agent_id})

    reachable = await external_agent_adapter.verify_connection(
        endpoint=agent.endpoint,
        api_key=api_key,
        protocol=agent.protocol,
        allowed_cidrs=allowed_cidrs,
    )
    agent.is_reachable = reachable
    agent.last_checked_at = datetime.now(timezone.utc)

    # NAP 协议：连通后从 /meta 同步 capabilities 和 description
    if agent.protocol == "nap" and reachable:
        try:
            meta = await external_agent_adapter.fetch_meta(
                endpoint=agent.endpoint, api_key=api_key, allowed_cidrs=allowed_cidrs,
            )
            if meta.get("capabilities"):
                agent.capabilities = json.dumps(meta["capabilities"], ensure_ascii=False)
            if meta.get("description"):
                agent.description = meta["description"]
        except Exception:
            logger.warning("NAP /meta fetch failed for agent %s, skipping meta sync", agent_id)

    await db.commit()
    await hooks.emit("operation_audit", action="external_agent.synced", target_type="external_agent", target_id=agent_id, actor_id=user.id, org_id=org.id, details={"reachable": reachable})
    return ApiResponse(data={"reachable": reachable, "agent_id": agent_id})


# ── Attachments ────────────────────────────────────────────────────────────────

MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB


@router.post("/{agent_id}/attachments/upload", response_model=ApiResponse[dict])
async def upload_attachment(
    agent_id: str,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """上传聊天附件（图片或文件），返回 storage_key 和临时预签名 URL。

    URL 仅供本次发送使用，不会持久化到数据库。
    """
    _, org = auth
    await external_agent_service.get_external_agent(agent_id=agent_id, org_id=org.id, db=db)

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 20MB 限制")

    storage_key = await storage_service.upload_external_agent_file(
        file_content=content,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        org_id=org.id,
    )
    url = await storage_service.get_presigned_url(storage_key)

    return ApiResponse(data={
        "storage_key": storage_key,
        "name": file.filename,
        "size": len(content),
        "content_type": file.content_type,
        "url": url,
    })


# ── Tool 型插件：/form、/invoke、/files（spec §6.2 / §8）─────────────────────


@router.get("/{agent_id}/form", response_model=ApiResponse[dict])
async def get_plugin_form(
    agent_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """返回 tool 型插件的 input_schema（仅 status=active 且用户有权限时）。

    兼容代理（spec §7.4）：内部代理到 sort_order=0 default function；Phase 1
    老客户端继续调此 URL 时行为不变，只是响应头加 Deprecation / Sunset，提示
    下个版本迁移到 /functions/{function_id}/form。

    返回字段包含 input_schema / output_hint / invoke_config（脱敏后） / version。
    invoke_config.auth.token 在响应中以 ***redacted*** 形式返回，绝不外泄明文。
    非 active 状态返回 403，message_key 为 errors.external_agent.not_active。
    """
    # 兼容代理响应头：spec §7.4 下个版本移除该端点。Sunset 暂用 RFC 7231 兼容格式。
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2030-01-01"

    _, org = auth
    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    if (agent.type or "chat") != "tool":
        raise BadRequestError(
            message="该插件不是 tool 类型",
            message_key="errors.external_agent.manifest_invalid",
        )
    if agent.status != "active":
        raise ForbiddenError(
            message="插件未启用",
            message_key="errors.external_agent.not_active",
        )

    # 内部代理到 sort_order=0 default function（spec §7.4）
    func = await external_agent_service.get_default_function_for_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    invoke_config = func.invoke_config or {}
    output_hint = (
        invoke_config.pop("_output_hint", None)
        if isinstance(invoke_config, dict) else None
    )

    return ApiResponse(data={
        "agent_id": agent.id,
        "version": func.version,
        "is_reachable": agent.is_reachable,
        "input_schema": func.input_schema,
        "output_hint": output_hint,
        "invoke_config": redact_invoke_config(invoke_config),
    })


@router.post("/{agent_id}/invoke", response_model=ApiResponse[dict])
async def invoke_plugin(
    agent_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """表单直连调用 tool 型插件（spec §6.2 主流程）。

    兼容代理（spec §7.4）：内部代理到 sort_order=0 default function；Phase 1
    老客户端继续调此 URL 时行为不变（零破坏），响应头加 Deprecation / Sunset。

    处理流程：
      1. 限流（spec §9.5，先于鉴权 / status / reachability 防 DoS 向量）
      2. 权限 + type=tool + status=active + is_reachable 检查（不可达 → 503）
      3. 取 sort_order=0 default function；其 input_schema/invoke_config 取代 agent 列
      4. 动态 pydantic 模型校验参数（缺必填/类型不符 → 422 字段级错误）
      5. 按 pass_mode 装配外部请求（multipart 流式转发 / url_ref 引用）
      6. httpx 代理调用（trust_env=False）+ auth 注入
      7. 响应处理：2xx → {success, data, display, items_path}；非 2xx → {success:false, upstream_status, error}
      8. 写审计日志（spec §9.6）

    外部返回内容仅作为数据，前端按 JSON/table 渲染，绝不作为 HTML 插入。
    """
    # 兼容代理响应头：spec §7.4 下个版本移除该端点
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2030-01-01"

    user, org = auth

    # 限流检查（spec §9.5 + §6.2 step 1）：先于鉴权 / status / reachability，
    # 未授权 / 越权请求也应受同一令牌桶约束，避免被滥用为拒绝服务向量。
    await external_agent_rate_limit.check_rate_limit(
        agent_id=agent_id, user_id=str(user.id),
    )

    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    if (agent.type or "chat") != "tool":
        raise BadRequestError(
            message="该插件不是 tool 类型",
            message_key="errors.external_agent.manifest_invalid",
        )
    if agent.status != "active":
        raise ForbiddenError(
            message="插件未启用",
            message_key="errors.external_agent.not_active",
        )
    if not agent.is_reachable:
        raise HTTPException(
            status_code=503,
            detail={
                "code": 50300,
                "error_code": 50300,
                "message_key": "errors.external_agent.invoke_unreachable",
                "message": "外部服务暂不可达",
            },
        )

    try:
        body = await request.json()
    except Exception:
        raise BadRequestError(
            message="请求体必须为 JSON",
            message_key="errors.external_agent.invoke_validation_error",
        )
    submit_params = body.get("params") if isinstance(body, dict) else None
    if not isinstance(submit_params, dict):
        raise BadRequestError(
            message="提交参数格式不正确",
            message_key="errors.external_agent.invoke_validation_error",
        )

    # 兼容代理：取 sort_order=0 default function（spec §7.4）。Phases 1 客户端
    # 通过这里间接调用新函数的 invoke_config / input_schema；零行为破坏。
    func = await external_agent_service.get_default_function_for_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    try:
        result = await invoke_tool(
            agent=agent,
            org_id=org.id,
            user_id=str(user.id),
            submit_params=submit_params,
            db=db,
            allowed_cidrs=await _get_org_allowed_cidrs(org.id, db),
            function_invoke_config=func.invoke_config,
            function_input_schema=func.input_schema,
        )
    except _ValidationError422 as exc:
        # 用 AppException 而非 HTTPException：全局 handler 会保留 extra
        # 字段，HTTPException 的 handler 会把不在白名单的字段（字段错）抹掉。
        raise AppException(
            code=42200,
            error_code=42200,
            message="提交参数不合法",
            message_key="errors.external_agent.invoke_validation_error",
            status_code=422,
            extra={"field_errors": exc.field_errors},
        )
    except _Unreachable503 as exc:
        # 不可达也是一次真实发生的调用尝试，落历史（success=false）供用户回看
        await external_agent_invocation_service.record_invocation(
            agent_id=agent_id, org_id=org.id, user_id=str(user.id),
            function_id=func.id, function_name=func.name,
            params=submit_params,
            result={"success": False, "error": exc.message},
            db=db,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": 50300,
                "error_code": 50300,
                "message_key": exc.message_key,
                "message": exc.message,
            },
        )

    # 成功 / 上游非 2xx（success:false，HTTP 200）都落一条调用历史（function 级
    # invoke 路径同款）；422 参数校验失败未发起外部调用，不记录。
    await external_agent_invocation_service.record_invocation(
        agent_id=agent_id, org_id=org.id, user_id=str(user.id),
        function_id=func.id, function_name=func.name,
        params=submit_params, result=result, db=db,
    )

    # 上游非 2xx 在 invoke_tool 内部已包成 success:false，这里再统一把"上游失败"
    # 用 message_key 透传给前端，方便 UI 提示（i18n）。
    if result.get("success") is False:
        return ApiResponse(data={
            **result,
            "message_key": "errors.external_agent.invoke_upstream_error",
        })
    return ApiResponse(data=result)


@router.post("/{agent_id}/files", response_model=ApiResponse[dict])
async def upload_plugin_form_file(
    agent_id: str,
    response: Response,
    file: UploadFile,
    field: str = "file",
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """tool 型表单的文件上传中转（spec §8）。

    兼容代理（spec §7.4）：内部从 sort_order=0 default function 的 input_schema
    找 file 字段；Phase 1 老客户端继续调此 URL 时行为不变，响应头加 Deprecation。

    仅 tool 型 Agent 接受；调用方须在 input_schema 中声明至少一个 file 类型字段。
    大小/类型白名单在此处就拦截（spec §8「不要等到 invoke」），避免污染 invoke 链路。
    TTL 24h，与 spec §8 一致。
    """
    # 兼容代理响应头：spec §7.4 下个版本移除该端点
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2030-01-01"

    _, org = auth
    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    if (agent.type or "chat") != "tool":
        raise BadRequestError(
            message="非 tool 型插件不接受文件上传",
            message_key="errors.external_agent.manifest_invalid",
        )
    if agent.status != "active":
        raise ForbiddenError(
            message="插件未启用",
            message_key="errors.external_agent.not_active",
        )

    # 兼容代理：从 sort_order=0 default function 的 input_schema 找 file 字段
    func = await external_agent_service.get_default_function_for_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )
    func_input_schema = func.input_schema or {}

    # 仅当 input_schema 含 file 字段才允许上传（避免对纯字段表单误用此端点）
    if not has_file_field(func_input_schema):
        raise BadRequestError(
            message="当前插件无文件字段",
            message_key="errors.external_agent.invoke_validation_error",
        )

    # 找到目标字段定义（按 field 名查表，未指定时取第一个 file 字段）
    file_fields = [
        (name, field_def) for name, field_def in (
            func_input_schema.get("fields", {}).items()
            if isinstance(func_input_schema, dict) else []
        )
        if field_def.get("type") == "file"
    ]
    if not file_fields:
        raise BadRequestError(
            message="当前插件无文件字段",
            message_key="errors.external_agent.invoke_validation_error",
        )

    target_field_def: dict | None = None
    if field and field != "file":
        for name, fdef in file_fields:
            if name == field:
                target_field_def = fdef
                break
    if target_field_def is None:
        target_field_def = file_fields[0][1]

    content = await file.read()
    accept = target_field_def.get("accept") if isinstance(target_field_def, dict) else None
    max_mb = target_field_def.get("max_mb") if isinstance(target_field_def, dict) else None

    result = await upload_plugin_file(
        file_content=content,
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        agent=agent,
        field_accept=accept,
        max_mb=max_mb,
        org_id=org.id,
    )
    return ApiResponse(data=result)


# ── Sessions ────────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/sessions", response_model=ApiResponse[list[ChatSessionResponse]])
async def list_sessions(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """列出当前用户在指定 Agent 下的所有会话，按最后更新时间倒序。"""
    user, org = auth
    await external_agent_service.get_external_agent(agent_id=agent_id, org_id=org.id, db=db)
    sessions = await external_agent_chat_service.list_sessions(
        agent_id=agent_id, user_id=str(user.id), db=db
    )
    return ApiResponse(data=[ChatSessionResponse.model_validate(s) for s in sessions])


@router.post("/{agent_id}/sessions", response_model=ApiResponse[ChatSessionResponse])
async def create_session(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """创建新聊天会话（title 为空，发送首条消息后自动填充）。"""
    user, org = auth
    await external_agent_service.get_external_agent(agent_id=agent_id, org_id=org.id, db=db)
    session = await external_agent_chat_service.create_session(
        agent_id=agent_id, org_id=org.id, user_id=str(user.id), db=db
    )
    return ApiResponse(data=ChatSessionResponse.model_validate(session))


@router.delete("/{agent_id}/sessions/{session_id}", response_model=ApiResponse[None])
async def delete_session(
    agent_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """软删除指定会话（仅会话归属用户可操作）。"""
    user, _ = auth
    await external_agent_chat_service.delete_session(
        session_id=session_id, user_id=str(user.id), agent_id=agent_id, db=db
    )
    return ApiResponse(data=None)


# ── Messages ────────────────────────────────────────────────────────────────────

@router.get(
    "/{agent_id}/sessions/{session_id}/messages",
    response_model=ApiResponse[list[MessageResponse]],
)
async def list_messages(
    agent_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """返回会话内全部消息，用户消息的附件实时注入预签名 URL。"""
    user, _ = auth
    chat_session = await external_agent_chat_service.get_session(
        session_id=session_id, user_id=str(user.id), agent_id=agent_id, db=db
    )
    if not chat_session:
        raise HTTPException(status_code=404, detail="会话不存在")

    messages = await external_agent_chat_service.get_messages(session_id=session_id, db=db)

    result: list[MessageResponse] = []
    for msg in messages:
        attachments_with_url: list[AttachmentItemWithUrl] | None = None
        if msg.attachments:
            attachments_with_url = []
            for att in msg.attachments:
                url = await storage_service.get_presigned_url(att["storage_key"])
                attachments_with_url.append(AttachmentItemWithUrl(**att, url=url))
        result.append(MessageResponse(
            id=msg.id,
            session_id=msg.session_id,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            attachments=attachments_with_url,
            created_at=msg.created_at,
        ))

    return ApiResponse(data=result)


# ── Chat（SSE 代理）───────────────────────────────────────────────────────────

@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth=Depends(get_current_org),
):
    """向外部 Agent 发起聊天，通过 SSE 流式返回响应（所有登录用户可用）。

    请求体：
      { "message": "用户消息", "session_id": "UUID", "attachments": [...] }

    SSE 事件格式：
      data: {"chunk": "文本片段"}\n\n
      data: {"done": true}\n\n
      data: {"error": "错误信息"}\n\n
    """
    user, org = auth

    # 限流检查（spec §9.5 "每插件每用户"）：chat 是最容易被滥用的入口，
    # 在 body 解析与会话归属校验之前先做令牌桶检查，避免扫号攻击拖慢服务。
    await external_agent_rate_limit.check_rate_limit(
        agent_id=agent_id, user_id=str(user.id),
    )

    body = await request.json()
    message: str = body.get("message", "")
    session_id: str = body.get("session_id", "")
    raw_attachments: list[dict] | None = body.get("attachments")

    agent = await external_agent_service.get_external_agent(
        agent_id=agent_id, org_id=org.id, db=db
    )

    # 会话归属校验：session_id 不存在或不属于当前用户一律 403，且与"不存在"
    # 返回同样的响应，避免被用来探测其他用户的 session_id 是否存在。
    chat_session = await external_agent_chat_service.get_session(
        session_id=session_id, user_id=str(user.id), agent_id=agent_id, db=db
    )
    if not chat_session:
        raise ForbiddenError(
            message="会话不存在或无权访问",
            message_key="errors.external_agent.session_forbidden",
        )

    # attachments 结构校验 + storage_key 归属校验：这两项都是不可信输入。
    # 结构上先用 Pydantic 模型兜底，避免下标取值在字段缺失/类型不对时抛 500；
    # storage_key 必须落在当前组织自己的外部 Agent 附件命名空间内，否则任何登录
    # 用户都能塞一个别人（甚至别的组织）的 storage_key 进来，后续 list_messages
    # 会拿它去签发预签名/HMAC 下载 URL，等同任意文件读取。
    attachments: list[AttachmentItemWithUrl] | None = None
    if raw_attachments:
        try:
            attachments = [AttachmentItemWithUrl(**a) for a in raw_attachments]
        except (TypeError, ValidationError) as exc:
            raise BadRequestError(
                message="附件格式不正确",
                message_key="errors.external_agent.invalid_attachment",
            ) from exc
        for att in attachments:
            if not external_agent_service.is_attachment_key_owned_by_org(
                att.storage_key, org.id
            ):
                raise ForbiddenError(
                    message="附件不属于当前组织，禁止引用",
                    message_key="errors.external_agent.attachment_forbidden",
                )

    api_key = external_agent_service.get_decrypted_api_key(agent)
    allowed_cidrs = await _get_org_allowed_cidrs(org.id, db)

    # 构建发给外部 Agent 的用户消息内容（附件以 URL 引用追加）
    user_content = message
    if attachments:
        file_lines = [
            f"- {a.name} ({a.content_type}, {a.size // 1024}KB): {a.url}"
            for a in attachments
        ]
        user_content += "\n\n附件:\n" + "\n".join(file_lines)

    # 从 DB 加载历史消息，构建完整 messages 列表
    # 跳过空 content 消息——上一轮 agent 无返回时会留下空 assistant 记录，
    # 发给 OpenAI-compatible API 会触发 "content cannot be empty" 拒绝。
    history = await external_agent_chat_service.get_messages(session_id=session_id, db=db)
    messages_for_agent = [{"role": m.role, "content": m.content} for m in history if m.content]
    messages_for_agent.append({"role": "user", "content": user_content})

    # 仅保存 storage_key，不保存 URL（URL 有有效期）
    attachments_to_save: list[dict] | None = None
    if attachments:
        attachments_to_save = [
            {
                "name": a.name,
                "size": a.size,
                "content_type": a.content_type,
                "storage_key": a.storage_key,
            }
            for a in attachments
        ]

    # rag_standard + external session_managed_by：传入已有的外部 session_id 供适配器
    # 直发请求；首次聊天或上一次 chat 流中通过 on_external_session_reset 写入的新映射
    # 都会同步更新到这里。非 rag_standard 协议忽略此字段。
    effective_external_sid = chat_session.external_session_id
    # rag_standard 但非 external 管理（如 platform/internal 模式）：保持 None，由适配器
    # 每次自生成 plat_{session_id} 后缀，无需在 DB 维护映射。
    if agent.protocol != "rag_standard" or agent.session_managed_by != "external":
        effective_external_sid = None

    # 外部会话失效自动重建回调：仅在事务里更新本会话的 external_session_id。
    # 重放只发一次，若新会话仍走不通（极端边界）会抛错由下方 catch 转 SSE error。
    async def _on_external_session_reset(new_sid: str) -> None:
        try:
            async with async_session_factory() as reset_db:
                result = await reset_db.execute(
                    select(ExternalAgentChatSession).where(
                        ExternalAgentChatSession.id == chat_session.id
                    )
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    row.external_session_id = new_sid
                    await reset_db.commit()
                logger.info(
                    "rag_standard 外部会话已重建并回写映射：session=%s external=%s",
                    chat_session.id, new_sid,
                )
        except Exception as reset_exc:
            # 映射回写失败仅记日志，重放仍然继续，避免流被打断
            logger.warning(
                "Failed to persist reset external_session_id for session %s: %s",
                chat_session.id, reset_exc,
            )

    collected_chunks: list[str] = []
    collected_thinking: list[str] = []

    async def event_stream():
        try:
            async for event_type, content in external_agent_adapter.chat_stream(
                endpoint=agent.endpoint,
                api_key=api_key,
                protocol=agent.protocol,
                messages=messages_for_agent,
                session_id=session_id,
                user_id=str(user.id),
                organization_id=str(org.id),
                external_session_id=effective_external_sid,
                on_external_session_reset=(
                    _on_external_session_reset if agent.protocol == "rag_standard"
                    and agent.session_managed_by == "external" else None
                ),
                allowed_cidrs=allowed_cidrs,
            ):
                if event_type == "thinking":
                    # 推理链路不计入 assistant 正式回复，单独发给前端展示并持久化
                    collected_thinking.append(content)
                    yield f"data: {json.dumps({'thinking': content}, ensure_ascii=False)}\n\n"
                elif event_type == "done":
                    # rag_standard 的 done 事件携带完整 answer，覆盖前面增量片段作为兜底
                    collected_chunks[:] = [content]
                    yield f"data: {json.dumps({'chunk': content}, ensure_ascii=False)}\n\n"
                else:
                    collected_chunks.append(content)
                    yield f"data: {json.dumps({'chunk': content}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            # exc_info=True 打印完整堆栈，便于排查协议解析失败等问题
            logger.warning("External agent chat error: %s %s", agent_id, exc, exc_info=True)
            # httpx 超时类异常的 str() 为空，给出人类可读的提示
            if isinstance(exc, httpx.ConnectTimeout):
                error_msg = f"连接超时：无法在 10s 内连接到外部 Agent（{agent.endpoint}），请确认服务是否运行"
            elif isinstance(exc, httpx.ReadTimeout):
                error_msg = "读取超时：外部 Agent 响应超过 120s"
            elif isinstance(exc, httpx.ConnectError):
                error_msg = f"连接失败：无法连接到外部 Agent（{agent.endpoint}）"
            else:
                error_msg = str(exc) or "外部 Agent 通信异常（无错误详情）"
            yield f"data: {json.dumps({'error': error_msg}, ensure_ascii=False)}\n\n"
        finally:
            yield f"data: {json.dumps({'done': True})}\n\n"
            assistant_content = "".join(collected_chunks)
            assistant_thinking = "".join(collected_thinking) or None
            # 只在 agent 有实际返回内容时才持久化，避免空 assistant 消息污染历史记录
            if session_id and message and assistant_content:
                asyncio.create_task(
                    _persist_messages(
                        session_id=session_id,
                        user_content=message,
                        user_attachments=attachments_to_save,
                        assistant_content=assistant_content,
                        assistant_thinking=assistant_thinking,
                    )
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
