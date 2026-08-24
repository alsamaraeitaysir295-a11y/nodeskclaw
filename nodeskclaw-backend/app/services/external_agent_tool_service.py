"""外部智能体 tool 型插件：表单动态校验 + invoke 代理 + 文件上传中转。

承担方案 §6.2（/{id}/form、/invoke、/files）的业务逻辑，与 chat 型解耦：
- chat 型：复用 external_agent_adapter.chat_stream
- tool 型：本模块处理 pydantic 动态模型构建、auth 注入、pass_mode 分发

SSRF 防护（task #5）与每插件限流（task #6）尚未接入，本模块用 TODO 标注，
待后续批量实施时再回来填，调用点结构已对齐。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks
from app.core.security import decrypt_sensitive
from app.models.external_agent import ExternalAgent
from app.schemas.external_agent import (
    ManifestInputSchema,
    ManifestInvokeAuth,
    ManifestInvokeConfig,
    ManifestOutputHint,
    build_dynamic_input_model,
)
from app.services import storage_service

logger = logging.getLogger(__name__)


# ── file extension 白名单（与 spec §4 input_schema.fields[file].accept 配合） ──

# 缺失说明：前端在 manifest 里按需声明 .xlsx / .csv 等 accept 列表，
# 本模块在 /{id}/files 阶段就已做白名单拦截（spec §8）—— 不需要全局默认，
# 调用方直接传 accept 数组进入函数即可。


def _decrypt_invoke_auth_token(invoke: Any) -> tuple[str, str | None]:
    """从 invoke_config 中取出解密后的 token，返回 (auth_type, token_plaintext)。

    invoke_config.auth.token 落库时已被 encrypt_sensitive 加密（service 层）；
    任何要外发的请求都要先解密再注入 header，绝不能以密文形式外发（spec §9.1）。

    invoke 可以是 dict（来自原始 DB JSON 列）或 ManifestInvokeConfig（Pydantic
    校验后实例），本函数兼容两种形态——同一处代码会被两个调用点使用。
    """
    auth = _auth_part(invoke) or {}
    auth_type = auth.get("type") if isinstance(auth, dict) else getattr(auth, "type", None)
    auth_type = auth_type or "none"
    token = auth.get("token") if isinstance(auth, dict) else getattr(auth, "token", None)
    if token and auth_type in ("bearer", "api_key_header"):
        try:
            token = decrypt_sensitive(token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Decrypt invoke auth token failed: %s", exc)
            token = None
    return auth_type, token


def _auth_part(invoke: Any) -> Any:
    if isinstance(invoke, dict):
        return invoke.get("auth")
    return getattr(invoke, "auth", None)


def _build_auth_headers(invoke: Any) -> dict[str, str]:
    """按 invoke.auth 规则生成 header 注入字典，token 必须是解密后的明文。"""
    auth_type, token = _decrypt_invoke_auth_token(invoke)
    headers: dict[str, str] = {}
    if auth_type == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "api_key_header" and token:
        auth = _auth_part(invoke)
        header_name = auth.get("header_name") if isinstance(auth, dict) else getattr(auth, "header_name", None)
        if header_name:
            headers[header_name] = token
    return headers


def _is_extension_allowed(filename: str, accept: list[str] | None) -> bool:
    """accept 列表形如 ['.xlsx', '.csv']；空/None 表示不限制。

    用 pathlib 提取扩展名（含前导点）后比对，匹配任一即放行。注意大小写不敏感。
    """
    if not accept:
        return True
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    if not ext:
        # 文件没有扩展名，且声明了白名单：拒绝（避免 .exe/.sh 等上传）
        return False
    return ext in {a.lower() for a in accept}


def _resolve_items_path(payload: Any, items_path: str | None) -> Any:
    """按点号路径定位数组；找不到则降级返回 payload 本身。

    output_hint.items_path 形如 'data'、'result.items'、''（空 = 顶层）。
    前端用此定位表格数组，但服务端这里只做无副作用提取供前端兜底展示。
    """
    if not items_path:
        return payload
    cur: Any = payload
    for segment in items_path.split("."):
        if isinstance(cur, dict) and segment in cur:
            cur = cur[segment]
        else:
            return payload
    return cur


def redact_invoke_config(invoke_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """GET /{id}/form 响应里给前端读的 invoke_config：脱敏 auth.token。"""
    if not invoke_config:
        return invoke_config
    redacted = {k: v for k, v in invoke_config.items()}
    auth = redacted.get("auth")
    if isinstance(auth, dict) and auth.get("token"):
        redacted["auth"] = {**auth, "token": "***redacted***"}
    return redacted


async def upload_plugin_file(
    *,
    file_content: bytes,
    filename: str,
    content_type: str,
    agent: ExternalAgent,
    field_accept: list[str] | None,
    max_mb: int | None,
    org_id: str,
) -> dict[str, Any]:
    """/{id}/files 业务实现：白名单拦截 + 大小限制 + 落对象存储。

    大小/类型白名单在此处就拦截（spec §8「不要等到 invoke」）。
    TTL 由 get_presigned_url 默认 3600s 提供，spec §8 要求 24h —— 这里统一 24h。
    """
    # 类型白名单
    if not _is_extension_allowed(filename, field_accept):
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            message="文件类型不在白名单内",
            message_key="errors.external_agent.file_type_not_allowed",
            message_params={"filename": filename},
        )

    # 大小限制（默认 20MB，与既有 attachments 上传保持一致；max_mb 给定时取其值）
    cap_mb = max_mb if max_mb is not None else 20
    if max_mb is not None and len(file_content) > max_mb * 1024 * 1024:
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            message="文件超过大小限制",
            message_key="errors.external_agent.file_too_large",
            message_params={"max_mb": str(max_mb)},
        )
    if max_mb is None and len(file_content) > 20 * 1024 * 1024:
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            message="文件超过大小限制",
            message_key="errors.external_agent.file_too_large",
            message_params={"max_mb": "20"},
        )

    storage_key = await storage_service.upload_external_agent_file(
        file_content=file_content,
        filename=filename,
        content_type=content_type,
        org_id=org_id,
    )
    # spec §8 要求 24h TTL；get_presigned_url 默认 3600s，按 86400s 覆写
    url = await storage_service.get_presigned_url(storage_key, expires=24 * 3600)

    # 审计：文件落档（spec §9.6）
    await hooks.emit(
        "operation_audit",
        action="external_agent.file_uploaded",
        target_type="external_agent",
        target_id=agent.id,
        details={
            "filename": filename,
            "size": len(file_content),
            "content_type": content_type,
            "storage_key": storage_key,
        },
    )

    return {
        "file_id": storage_key,
        "storage_key": storage_key,
        "name": filename,
        "size": len(file_content),
        "content_type": content_type,
        "url": url,
    }


def get_file_fields(input_schema: ManifestInputSchema | dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """从 input_schema 中枚举所有 type=file 的字段，返回 (name, field_def) 元组列表。"""
    fields = input_schema.fields if isinstance(input_schema, ManifestInputSchema) else (input_schema or {}).get("fields", {})
    out: list[tuple[str, dict[str, Any]]] = []
    for name, field in fields.items():
        ftype = getattr(field, "type", None) if not isinstance(field, dict) else field.get("type")
        if ftype == "file":
            out.append((name, field if isinstance(field, dict) else field.model_dump()))
    return out


def has_file_field(input_schema: ManifestInputSchema | dict[str, Any] | None) -> bool:
    return bool(input_schema) and bool(get_file_fields(input_schema))


async def invoke_tool(
    *,
    agent: ExternalAgent,
    org_id: str,
    user_id: str,
    submit_params: dict[str, Any],
    db: AsyncSession,
    allowed_cidrs: list[str] | None = None,
    function_invoke_config: dict[str, Any] | None = None,
    function_input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /{id}/invoke 与 /functions/{fid}/invoke 共用主逻辑：校验 → 装配 → 代理调用 → 包装响应。

    数据源优先级：function_invoke_config / function_input_schema（来自 Function 级调用）>
    agent.invoke_config / agent.input_schema（来自 Phase 1 / 兼容代理）。两个 kw-only
    入参为 None 时回落到 agent 列，零侵入保证旧调用点继续工作。

    返回值结构：
      成功：{success: True, data, display, items_path, raw_response}
      失败：{success: False, upstream_status, error}   （HTTP 200，由前端按 success 判定）
      不可达：抛 503（spec §6.2 第 1 步）
      校验失败：抛 422（spec §6.2 第 2 步）

    注：调用方负责捕获 422/503，本函数不直接 raise。
    """
    input_schema = (
        function_input_schema if function_input_schema is not None else agent.input_schema
    )
    invoke_config = (
        function_invoke_config
        if function_invoke_config is not None
        else (agent.invoke_config or {})
    )
    output_hint_raw = invoke_config.get("_output_hint") or {}

    # 1. 动态模型构建与校验
    try:
        schema_model = ManifestInputSchema.model_validate(input_schema)
    except ValidationError as exc:
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            message="input_schema 配置不合法",
            message_key="errors.external_agent.manifest_invalid",
        ) from exc
    dynamic_model = build_dynamic_input_model(schema_model)
    try:
        validated = dynamic_model.model_validate(submit_params)
    except ValidationError as exc:
        # 422 字段级错误：errors 列表原生形如 [{loc, msg, type, input}, ...]
        field_errors = [
            {
                "field": ".".join(str(x) for x in err.get("loc", [])),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        raise _ValidationError422(field_errors=field_errors) from exc

    validated_dict = validated.model_dump()

    # 2. 分离 file 字段与非 file 字段，按 pass_mode 决定如何把 file 传给外部
    invoke = ManifestInvokeConfig.model_validate(invoke_config)
    pass_mode = invoke.pass_mode

    # 普通字段（去掉 file 类型）
    file_field_names = {name for name, _ in get_file_fields(input_schema)}
    plain_fields: dict[str, Any] = {
        k: v for k, v in validated_dict.items() if k not in file_field_names
    }
    # daterange 序列化格式：动态模型已按 {"start": ..., "end": ...} 解析，直接透传
    file_meta: dict[str, dict[str, str]] = {}
    for name, field_def in get_file_fields(input_schema):
        if name in validated_dict and validated_dict[name]:
            file_id = validated_dict[name]
            url = await storage_service.get_presigned_url(file_id, expires=24 * 3600)
            file_meta[name] = {"file_id": file_id, "url": url, "name": path_basename_safe(file_id)}

    # 3. 装配外部请求
    method = invoke.method.upper()
    timeout = httpx.Timeout(invoke.timeout_seconds, connect=min(invoke.timeout_seconds, 10.0))
    headers = _build_auth_headers(invoke)
    endpoint = invoke.endpoint
    # SSRF 闸门 + WSL 重写（DEBUG-only）；私网/回环没在 allow-list 中时直接抛 ForbiddenError
    from app.services.external_agent_adapter import resolve_endpoint_for_request
    # org 级 allow-list 从 Organization.external_agent_allowed_cidrs 取，DB 读在 caller 处
    # 已加载；这里仅以 None 兜底——caller 路径会显式传入。
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs=allowed_cidrs)

    # TODO(task #6 限流): 按 (agent.id, user_id) 做令牌桶限流，10 次/分钟，超限返回 429。

    upstream_status: int | None = None
    upstream_body: bytes | None = None
    latency_ms = 0
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            if method == "GET":
                query_params: dict[str, Any] = {}
                for k, v in plain_fields.items():
                    if isinstance(v, (dict, list)):
                        query_params[k] = json.dumps(v, ensure_ascii=False)
                    elif v is not None:
                        query_params[k] = str(v)
                resp = await client.request(method, endpoint, params=query_params, headers=headers)
            elif pass_mode == "multipart":
                # multipart 模式：文件走 files=，普通字段走 data=
                files = []
                for name, meta in file_meta.items():
                    content = await _read_storage_file(meta["file_id"])
                    files.append((name, (meta["name"], content, _guess_content_type(meta["name"]))))
                resp = await client.request(method, endpoint, data=plain_fields, files=files, headers=headers)
            else:  # url_ref
                # url_ref 模式：普通字段进 JSON body，文件以 {file_id, url, name} 引用注入
                body = dict(plain_fields)
                for name, meta in file_meta.items():
                    body[name] = meta
                resp = await client.request(
                    method, endpoint,
                    json=body,
                    headers={**headers, "Content-Type": "application/json"},
                )
        upstream_status = resp.status_code
        upstream_body = resp.content
    except httpx.TimeoutException as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        await _write_audit(
            action="external_agent.invoked",
            agent_id=agent.id, user_id=user_id, org_id=org_id,
            details={"upstream_status": None, "ok": False, "latency_ms": latency_ms,
                     "error": f"timeout: {exc!s}"},
        )
        raise _Unreachable503(
            message="外部服务暂不可达（连接超时）",
            message_key="errors.external_agent.invoke_unreachable",
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        await _write_audit(
            action="external_agent.invoked",
            agent_id=agent.id, user_id=user_id, org_id=org_id,
            details={"upstream_status": None, "ok": False, "latency_ms": latency_ms,
                     "error": type(exc).__name__},
        )
        raise _Unreachable503(
            message="外部服务暂不可达（连接失败）",
            message_key="errors.external_agent.invoke_unreachable",
        )
    latency_ms = int((time.monotonic() - start) * 1000)

    # 4. 响应处理
    success = 200 <= upstream_status < 300
    if not success:
        await _write_audit(
            action="external_agent.invoked",
            agent_id=agent.id, user_id=user_id, org_id=org_id,
            details={
                "upstream_status": upstream_status,
                "ok": False,
                "latency_ms": latency_ms,
                "params_summary": _params_summary(plain_fields, file_meta),
            },
        )
        # 非 2xx：包成 success:false 但 HTTP 仍 200（spec §6.2 第 5 步；
        # 不抛 5xx，避免前端被代理误判为平台错误）
        return {
            "success": False,
            "upstream_status": upstream_status,
            "latency_ms": latency_ms,
            "error": _extract_error_message(upstream_body, upstream_status),
        }

    # 2xx：尝试按 JSON 解析；解析失败时降级返回 raw 文本
    data: Any
    try:
        data = json.loads(upstream_body or b"null")
    except (ValueError, json.JSONDecodeError):
        data = (upstream_body or b"").decode("utf-8", errors="replace")

    output_hint = ManifestOutputHint.model_validate(output_hint_raw) if output_hint_raw else ManifestOutputHint()
    items_path = output_hint.items_path

    await _write_audit(
        action="external_agent.invoked",
        agent_id=agent.id, user_id=user_id, org_id=org_id,
        details={
            "upstream_status": upstream_status,
            "ok": True,
            "latency_ms": latency_ms,
            "params_summary": _params_summary(plain_fields, file_meta),
        },
    )

    # display=text：按 text_path 取字符串字段（如 RAG 的 answer），前端渲染 Markdown
    # upstream_status / latency_ms 同时给用户侧「调用历史」落库使用（invoke 响应附带，
    # 前端可忽略）
    result: dict[str, Any] = {
        "success": True,
        "upstream_status": upstream_status,
        "latency_ms": latency_ms,
        "data": data,
        "display": output_hint.display,
        "items_path": items_path,
        "resolved_items": _resolve_items_path(data, items_path),
    }
    if output_hint.display == "text":
        text_value: str | None = None
        cur: Any = data
        if output_hint.text_path:
            for segment in output_hint.text_path.split("."):
                if isinstance(cur, dict) and segment in cur:
                    cur = cur[segment]
                else:
                    cur = None
                    break
        if isinstance(cur, str):
            text_value = cur
        elif isinstance(cur, dict) and isinstance(cur.get("answer"), str):
            # 未配 text_path 时兜底取 answer（RAG 类问答的通用约定）
            text_value = cur["answer"]
        result["text"] = text_value
    return result


# ── helpers ────────────────────────────────────────────────────────────────


def _extract_error_message(body: bytes | None, status: int) -> str:
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return str(parsed.get("message") or parsed.get("error") or parsed)
        except (ValueError, json.JSONDecodeError):
            pass
        text = body.decode("utf-8", errors="replace").strip()
        if text:
            return text[:500]
    return f"Upstream HTTP {status}"


def _params_summary(plain: dict[str, Any], files: dict[str, dict[str, str]]) -> dict[str, Any]:
    """审计摘要：字段名 + 类型 + 大小，不写明文值。"""
    summary: dict[str, Any] = {}
    for k, v in plain.items():
        if isinstance(v, (dict, list)):
            summary[k] = f"{type(v).__name__}({len(v)})"
        elif v is None:
            summary[k] = "null"
        else:
            summary[k] = type(v).__name__
    for k, meta in files.items():
        summary[k] = f"file:{meta.get('name', '?')}"
    return summary


async def _write_audit(
    *,
    action: str,
    agent_id: str,
    user_id: str,
    org_id: str,
    details: dict[str, Any],
) -> None:
    try:
        await hooks.emit(
            "operation_audit",
            action=action,
            target_type="external_agent",
            target_id=agent_id,
            actor_id=user_id,
            org_id=org_id,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit emit failed (%s): %s", action, exc)


async def _read_storage_file(storage_key: str) -> bytes:
    """读 storage 上的文件本体（multipart 转发用）。"""
    from app.services import storage_service
    try:
        return await storage_service.download_file(storage_key)
    except Exception:
        return await storage_service.download_raw(storage_key)


def _guess_content_type(filename: str) -> str:
    import mimetypes
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


def path_basename_safe(storage_key: str) -> str:
    """从 storage_key 末尾抽文件名（仅显示用，不参与路径解析）。"""
    from pathlib import PurePosixPath
    return PurePosixPath(storage_key).name


# ── 自定义异常：422 / 503 业务错误，调用方捕获后转 HTTPException ──


class _ValidationError422(Exception):
    """spec §6.2 第 2 步：字段级 422。"""

    def __init__(self, *, field_errors: list[dict[str, str]]):
        self.field_errors = field_errors


class _Unreachable503(Exception):
    """spec §6.2 第 1 步：上游不可达 → 503 友好错误。"""

    def __init__(self, *, message: str, message_key: str):
        self.message = message
        self.message_key = message_key
