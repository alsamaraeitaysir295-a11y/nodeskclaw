"""external_agent_adapter.py：与外部 Agent 服务通信的 HTTP 适配器。

支持协议：
  - openai_compatible：标准 OpenAI Chat Completions API（POST /v1/chat/completions）
  - custom：mom_agent 兼容格式（POST /chat，SSE 命名事件）
  - nap：NoDeskClaw Agent Protocol v1.0（POST /stream，SSE 命名事件）
  - rag_standard：内网 RAG 标准问答协议（POST /api/v1/agent/stream，SSE；
    详见方案附录 A 与 ee/docs/外部智能体一期方案.md）
"""

import json
import logging
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

import httpx

from app.core.config import settings
from app.schemas.external_agent import ManifestInvokeConfig

logger = logging.getLogger(__name__)

# 连接验证超时（秒）
_VERIFY_TIMEOUT = 10.0
# 聊天流式超时：connect 10s，总 120s（等待 LLM 推理）
_CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0)
# rag_standard 会话管理（POST /sessions / DELETE /history）使用同一连接级超时
_SESSION_TIMEOUT = 15.0


class ExternalSessionLostError(Exception):
    """rag_standard 协议专属：外部服务指示"会话不存在/已失效"。

    由 chat_stream 检测到 404 / error 事件 {type:'error', error:'session not found'*} 时抛出，
    上层应捕获并触发"自动重建外部会话 + 重放当前问题"降级路径，见方案附录 A 隔离规则 4。
    """


def compute_platform_external_session_id(platform_session_id: str) -> str:
    """按方案附录 A 隔离规则 1 生成外部 session_id：`plat_{平台会话id}` 格式。

    平台自生成的全局唯一 ID，绝不复用外部文档示例中的固定值（如 `default`），
    否则所有用户共享同一外部上下文，造成跨用户数据串扰。
    """
    return f"plat_{platform_session_id}"


def _get_wsl_host_ip() -> str | None:
    """获取 WSL 宿主机（Windows）的可路由 IP 地址。

    优先从默认路由网关获取：`ip route show default` → 第三字段（via <IP>）。
    这是 WSL2 NAT 网络下 Windows 宿主机的实际地址（通常 172.x.x.1）。
    /etc/resolv.conf nameserver 是 DNS 虚拟 IP，不能用于建立 TCP 连接，仅作备用。
    """
    # 方法1：从默认路由网关获取（最可靠）
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            # 格式：default via <gateway_ip> dev eth0 ...
            if len(parts) >= 3 and parts[0] == "default" and parts[1] == "via":
                return parts[2]
    except Exception:
        pass

    # 方法2：回退到 /etc/resolv.conf nameserver（旧版 WSL 或 mirrored 模式）
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    ip = line.split()[1].strip()
                    # 过滤掉 WSL2 DNS stub（10.255.255.254）
                    if ip != "10.255.255.254":
                        return ip
    except OSError:
        pass

    return None


def _resolve_wsl_endpoint(endpoint: str) -> str:
    """WSL 环境下将 127.0.0.1/localhost 映射到 Windows 宿主机 IP。

    WSL2 NAT 模式下，127.0.0.1 是 WSL loopback，无法访问 Windows 服务。
    宿主机 IP 通过默认路由网关获取（`ip route show default`）。

    ⚠️ 生产禁用：仅当 `settings.DEBUG=true` 时才执行重写。Linux 服务器上没有
    "宿主机 IP" 可写，且 silent URL mutation 既是 bug 也是潜在安全洞，
    与 SSRF 防护方向相反（spec §9.2）。DEBUG 关闭时本函数为 no-op，
    调用方完全感知不到差异——所有外部请求按字面 host 发出。
    """
    # 每次调用重新读 settings.DEBUG（不是 import-time 常量），允许运行时切换。
    if not getattr(settings, "DEBUG", False):
        return endpoint

    # 检测是否在 WSL 环境中运行
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            osrelease = f.read().lower()
    except OSError:
        return endpoint

    if "microsoft" not in osrelease and "wsl" not in osrelease:
        return endpoint

    parsed = urlparse(endpoint)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return endpoint

    host_ip = _get_wsl_host_ip()
    if not host_ip:
        return endpoint

    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = f"{host_ip}{port}"
    resolved = urlunparse(parsed._replace(netloc=new_netloc))
    logger.info("WSL 环境：将 %s 重映射为 %s", endpoint, resolved)
    return resolved


def resolve_endpoint_for_request(
    endpoint: str,
    allowed_cidrs: list[str] | None = None,
) -> str:
    """所有 outbound HTTP 前的统一入口：先做 SSRF 检查，再做 WSL localhost 重写。

    调用顺序很重要（spec §9.2 强调）：
      1. SSRF 在前：即便 DEBUG=true 且管理员提交了 169.254.169.254，也必须被硬禁。
      2. WSL 重写在后：仅在 DEBUG=true 时把 127.0.0.1/localhost 改写到宿主机 IP。

    调用方应在每次向外部插件发起 HTTP 前调用本函数（参数 allowed_cidrs 来自
    当前 org 的 SSRF 白名单配置）。
    """
    from app.services.external_agent_ssrf import validate_invoke_endpoint
    validate_invoke_endpoint(endpoint, allowed_cidrs)
    return _resolve_wsl_endpoint(endpoint)


async def verify_connection(
    endpoint: str,
    api_key: str | None,
    protocol: str,
    allowed_cidrs: list[str] | None = None,
) -> bool:
    """验证外部 Agent 服务是否可达。

    openai_compatible：调用 GET /v1/models，不要求 200（401 也算可达，说明服务在线）
    custom：调用 GET {endpoint}/health，期望 200
    nap：调用 GET {endpoint}/health，期望响应体 {"status": "ok"}
    """
    # SSRF 闸门 + WSL 重写（DEBUG-only）；私网/回环没在 allow-list 中时直接抛 403
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        # trust_env=False：不走系统代理（HTTP_PROXY 等），直连 Agent 服务
        async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, trust_env=False) as client:
            if protocol == "openai_compatible":
                # /v1/models 可能返回 200（无鉴权）或 401（有鉴权），均视为服务可达
                resp = await client.get(
                    f"{endpoint}/v1/models",
                    headers=headers,
                )
                return resp.status_code in (200, 401, 403)
            elif protocol == "nap":
                # NAP 要求 /health 返回 {"status": "ok"}
                resp = await client.get(f"{endpoint}/health", headers=headers)
                if resp.status_code != 200:
                    return False
                try:
                    return resp.json().get("status") == "ok"
                except Exception:
                    return False
            elif protocol == "rag_standard":
                # rag_standard 的探活路径就是它的会话列表接口（GET /api/v1/agent/sessions）。
                # 与 /sync 端点的判定保持一致：200 视为可达，其余一律视为不在线。
                resp = await client.get(
                    f"{endpoint}/api/v1/agent/sessions",
                    params={"limit": 1},
                    headers=headers,
                )
                return resp.status_code == 200
            else:
                resp = await client.get(
                    f"{endpoint}/health",
                    headers=headers,
                )
                return resp.status_code == 200
    except Exception:
        logger.warning("External agent connection check failed: %s", endpoint, exc_info=True)
        return False


async def fetch_meta(
    endpoint: str,
    api_key: str | None,
    allowed_cidrs: list[str] | None = None,
) -> dict:
    """调用 NAP GET /meta，返回 Agent 元数据。

    用于 sync 端点自动同步 capabilities / description 等字段。
    """
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # trust_env=False：不走系统代理，直连 Agent 服务
    async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, trust_env=False) as client:
        resp = await client.get(f"{endpoint}/meta", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def probe_chat_endpoint(
    endpoint: str,
    api_key: str | None,
    protocol: str,
    allowed_cidrs: list[str] | None = None,
) -> dict:
    """Manifest 试调（chat 型）：按协议探活路径请求一次，返回详细结果供管理端展示。

    与 verify_connection 的区别：verify_connection 用于既有 /sync 端点，只关心
    "服务是否在线"这个布尔判断（401/403 也算在线）；这里是插件提交向导第 3 步的
    试调诊断，需要把 http_code / 耗时 / 错误原因都展示给管理员，判定也更严格
    （5xx 视为不可用）。
    """
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if protocol == "rag_standard":
        url = f"{endpoint}/api/v1/agent/sessions?limit=1"
    elif protocol == "openai_compatible":
        url = f"{endpoint}/v1/models"
    else:
        url = f"{endpoint}/health"

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, trust_env=False) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        ok = resp.status_code < 500
        return {
            "ok": ok,
            "http_code": resp.status_code,
            "latency_ms": latency_ms,
            "error": None if ok else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("Plugin manifest chat probe failed: %s", url, exc_info=True)
        return {
            "ok": False,
            "http_code": None,
            "latency_ms": latency_ms,
            "error": str(exc) or type(exc).__name__,
        }


async def probe_tool_invoke(
    invoke: ManifestInvokeConfig,
    allowed_cidrs: list[str] | None = None,
) -> dict:
    """Manifest 试调（tool 型）：GET 直接调一次；非 GET 因当前字段定义无 default
    属性、必填参数恒无默认值，按方案 §6.1 规则跳过实际调用，仅做一次 HEAD 请求
    验证 DNS/TCP 可达（收到任意 HTTP 响应即视为网络可达，参考 §10 健康检查判定
    405/404 也算可达的规则），返回结果里额外标注 skipped_invoke。
    """
    endpoint = resolve_endpoint_for_request(invoke.endpoint, allowed_cidrs)
    headers: dict[str, str] = {}
    if invoke.auth.type == "bearer" and invoke.auth.token:
        headers["Authorization"] = f"Bearer {invoke.auth.token}"
    elif invoke.auth.type == "api_key_header" and invoke.auth.token and invoke.auth.header_name:
        headers[invoke.auth.header_name] = invoke.auth.token

    method = "GET" if invoke.method == "GET" else "HEAD"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, trust_env=False) as client:
            resp = await client.request(method, endpoint, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        ok = resp.status_code < 500
        return {
            "ok": ok,
            "http_code": resp.status_code,
            "latency_ms": latency_ms,
            "error": None if ok else f"HTTP {resp.status_code}",
            "skipped_invoke": method == "HEAD",
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("Plugin manifest tool probe failed: %s", endpoint, exc_info=True)
        return {
            "ok": False,
            "http_code": None,
            "latency_ms": latency_ms,
            "error": str(exc) or type(exc).__name__,
            "skipped_invoke": method == "HEAD",
        }


async def chat_stream(
    endpoint: str,
    api_key: str | None,
    protocol: str,
    messages: list[dict],
    session_id: str | None = None,
    user_id: str | None = None,
    organization_id: str | None = None,
    external_session_id: str | None = None,
    on_external_session_reset: Callable[[str], Awaitable[None] | None] | None = None,
    allowed_cidrs: list[str] | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """向外部 Agent 发起流式聊天，逐块 yield (event_type, content) 元组。

    event_type 取值：
      "message"  — 正式回复文本片段
      "thinking" — 推理/思考过程片段（NAP 协议专属，其他协议不产生）

    openai_compatible：POST /v1/chat/completions，stream=True
    custom：POST {endpoint}/chat，兼容 mom_agent 命名 SSE 事件格式
    nap：POST {endpoint}/stream，NAP v1.0 标准格式
      响应支持 event: thinking（推理链路）/ event: message / event: done / event: error
    rag_standard：POST {endpoint}/api/v1/agent/stream，body {question, session_id, mode:"auto"}
      响应事件类型为 {type: ...}；详见方案附录 A。

    rag_standard 专属参数：
      external_session_id — 平台已映射的外部 session_id（chat_service 会预填）
      on_external_session_reset — 可选回调，签名 async (new_external_session_id: str) -> None；
        当流中发现外部会话已失效（ExternalSessionLostError），适配器自动用同一 plat_ 前缀
        新建一个外部会话并重放当前问题，完成后通过此回调把新映射回写数据库，
        调用方（API 层）负责持久化新映射。
    """
    # SSRF 闸门 + WSL 重写（DEBUG-only）
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if protocol == "openai_compatible":
        url = f"{endpoint}/v1/chat/completions"
        payload: dict = {"model": "default", "messages": messages, "stream": True}
        # trust_env=False：不走系统代理，直连 Agent 服务
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT, trust_env=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for chunk in _parse_openai_sse(resp):
                    yield ("message", chunk)

    elif protocol == "nap":
        url = f"{endpoint}/stream"
        payload = {
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "session_id": session_id or str(uuid.uuid4()),
            "user_id": user_id or "anonymous",
            "organization_id": organization_id,
            "messages": messages,
            "metadata": {"source": "nodeskclaw"},
        }
        # trust_env=False：不走系统代理，直连 Agent 服务
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT, trust_env=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for event_type, chunk in _parse_nap_sse(resp):
                    yield (event_type, chunk)

    elif protocol == "rag_standard":
        # 只取最后一条 user 消息当作本轮 question；RAG 服务自身按 session_id 维护上下文，
        # 不需要把整段 history 重新塞给它（这与 nap / openai 不同）。
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            messages[-1]["content"] if messages else "",
        )
        # external_session_id 兜底：未传时退化为 plat_{平台 session_id}，确保隔离规则 1。
        plat_session_id = session_id or str(uuid.uuid4())
        effective_external_sid = (
            external_session_id
            or compute_platform_external_session_id(plat_session_id)
        )
        async for event_type, content in _chat_stream_rag_standard(
            endpoint=endpoint,
            api_key=api_key,
            question=last_user_msg,
            external_session_id=effective_external_sid,
            headers=headers,
            on_session_reset=on_external_session_reset,
            allowed_cidrs=allowed_cidrs,
        ):
            yield (event_type, content)

    else:
        # custom 协议（mom_agent 格式）
        # 只取最后一条 user 消息发给 agent，session 记忆由 agent 内部维护
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            messages[-1]["content"] if messages else "",
        )
        url = f"{endpoint}/chat"
        payload = {
            "session_id": session_id or str(uuid.uuid4()),
            "user_id": user_id or "anonymous",
            "message": last_user_msg,
            "thinking": True,   # 启用推理链路，agent 返回 event: thought 片段
        }
        # trust_env=False：不走系统代理，直连 Agent 服务
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT, trust_env=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for event_type, chunk in _parse_named_sse(resp):
                    yield (event_type, chunk)


async def _parse_openai_sse(resp: httpx.Response) -> AsyncIterator[str]:
    """解析 OpenAI 兼容 SSE：data: {"choices":[{"delta":{"content":"..."}}]}"""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if raw == "[DONE]":
            return
        try:
            chunk = json.loads(raw)
            text = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if text:
                yield text
        except Exception:
            if raw:
                yield raw


async def _parse_nap_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """解析 NAP v1.0 SSE 命名事件，yield (event_type, content) 元组。

    格式：
        event: thinking
        data: 推理/思考过程文本片段  ← 透传给平台折叠展示

        event: message
        data: 文本片段

        event: tool_call
        data: {...}   ← 忽略，仅透传 thinking / message 事件

        event: done
        data: complete

        event: error
        data: {"code": "...", "message": "..."}
    """
    current_event: str | None = None
    async for line in resp.aiter_lines():
        if not line:
            # 空行 = 事件结束
            current_event = None
            continue

        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
            continue

        if line.startswith("data:"):
            raw = line[len("data:"):].strip()

            if current_event == "thinking":
                # 推理/思考过程，透传给平台折叠展示（不计入正式回复）
                if raw:
                    yield ("thinking", raw)

            elif current_event == "message":
                if raw:
                    yield ("message", raw)

            elif current_event == "done":
                return

            elif current_event == "error":
                try:
                    err = json.loads(raw)
                    # NAP 错误格式：{"code": "...", "message": "..."}
                    msg = err.get("message") or err.get("error", raw)
                    raise RuntimeError(msg)
                except json.JSONDecodeError:
                    raise RuntimeError(raw)


async def _parse_named_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """解析命名 SSE 事件（mom_agent / custom 格式），yield (event_type, content) 元组。

    格式：
        event: thought
        data: 思考过程文本片段  ← 透传为 thinking 类型

        event: answer
        data: 回答文本片段

        event: status
        data: {"stage": "...", "label": "...", "ok": true}  ← 忽略

        event: done
        data: {...}

        event: error
        data: {"error": "..."}
    """
    current_event: str | None = None
    async for line in resp.aiter_lines():
        if not line:
            # 空行 = 事件结束，重置当前事件类型
            current_event = None
            continue

        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
            continue

        if line.startswith("data:"):
            raw = line[len("data:"):].strip()

            if current_event == "thought":
                # 推理/思考过程，透传为 thinking 类型供平台折叠展示
                if raw:
                    yield ("thinking", raw)

            elif current_event == "answer":
                # answer 事件的 data 直接是文本片段（非 JSON）
                if raw:
                    yield ("message", raw)

            elif current_event == "done":
                return

            elif current_event == "error":
                try:
                    err = json.loads(raw)
                    raise RuntimeError(err.get("error", raw))
                except json.JSONDecodeError:
                    raise RuntimeError(raw)

            elif current_event is None:
                # 兜底：无 event 标记时降级为 data-only 解析
                if raw == "[DONE]":
                    return
                try:
                    chunk = json.loads(raw)
                    text = chunk.get("content", "")
                    if text:
                        yield ("message", text)
                except Exception:
                    if raw:
                        yield ("message", raw)


# ── rag_standard 协议专属：会话生命周期 + 流式 + 解析 ─────────────────────────────


# "会话失效"判定关键词。rag_standard 服务的 error 事件中可能用不同文案表达同一件事，
# 这里用小写子串匹配兜底，规避文档示例之外的措辞差异。
_SESSION_LOST_PATTERNS: tuple[str, ...] = (
    "session not found",
    "session does not exist",
    "session not exist",
    "session has expired",
    "session expired",
    "invalid session",
    "会话不存在",
    "会话已失效",
)


def _is_session_lost_message(message: str) -> bool:
    """判定外部 error 事件文案是否属于"会话失效"类。"""
    msg = (message or "").lower()
    return any(p in msg for p in _SESSION_LOST_PATTERNS)


async def create_external_session(
    endpoint: str,
    external_session_id: str,
    title: str,
    api_key: str | None,
    timeout: float = _SESSION_TIMEOUT,
    allowed_cidrs: list[str] | None = None,
) -> str:
    """向外部 RAG 服务注册新会话：POST {endpoint}/api/v1/agent/sessions。

    由 chat_service 在创建平台会话时同步调用，建立 `plat_xxx → 外部 session_id` 的
    持久化映射（见方案附录 A 隔离规则 1）。返回外部 session_id（通常与传入相同，
    因外部服务只做登记，不会生成新 id；即便不同时也以响应为准）。

    调用失败（HTTP 4xx/5xx、网络异常、解析失败）一律抛 RuntimeError；
    上层应决定是降级（存为 draft）还是直接报错——本函数不静默吞错。
    """
    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{endpoint}/api/v1/agent/sessions"
    body = {"session_id": external_session_id, "title": title}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(url, json=body, headers=headers)
    except Exception as exc:
        raise RuntimeError(
            f"外部 RAG 会话创建失败（POST {url}）：{exc or type(exc).__name__}"
        ) from exc

    if resp.status_code >= 400:
        raise RuntimeError(
            f"外部 RAG 会话创建失败：HTTP {resp.status_code} {resp.text[:200]}"
        )

    # 优先以响应体里的 session_id 字段为准（兼容外部实现可能返回规范化后的 id）；
    # 响应里没有时直接使用我们传入的 plat_xxx 值。
    try:
        data = resp.json()
        if isinstance(data, dict):
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    except Exception:
        # 响应体不是 JSON 也无所谓，外部即使 200 OK 也允许空体
        pass
    return external_session_id


async def delete_external_history(
    endpoint: str,
    external_session_id: str,
    api_key: str | None,
    timeout: float = _SESSION_TIMEOUT,
    allowed_cidrs: list[str] | None = None,
) -> None:
    """删除外部 RAG 历史：DELETE {endpoint}/api/v1/agent/history/{sid}。

    失败仅记录日志，不抛——删除平台会话是用户的本意，外部侧清理失败也不应阻塞
    本地软删除（用户在平台端已经"看不见"该会话了）。
    """
    if not external_session_id:
        return

    endpoint = resolve_endpoint_for_request(endpoint, allowed_cidrs)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # sid 直接拼在路径里。plat_ 前缀 + UUID 字符集足够安全，无需 URL encoding。
    url = f"{endpoint}/api/v1/agent/history/{external_session_id}"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.delete(url, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "External rag session history delete returned HTTP %s for %s",
                resp.status_code, external_session_id,
            )
    except Exception as exc:
        logger.warning(
            "External rag session history delete failed for %s: %s",
            external_session_id, exc or type(exc).__name__,
        )


async def _chat_stream_rag_standard(
    endpoint: str,
    api_key: str | None,
    question: str,
    external_session_id: str,
    headers: dict[str, str],
    on_session_reset: Callable[[str], Awaitable[None] | None] | None,
    allowed_cidrs: list[str] | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """rag_standard 协议的流式聊天 + 外部会话失效自动重建。

    第 1 次尝试（用现有 external_session_id）：如遇 ExternalSessionLostError，
    - 调外部 POST /api/v1/agent/sessions 用同一 plat_ 前缀重建会话；
    - 通过 on_session_reset 回调把新映射回写数据库；
    - 当前轮的增量数据全部丢弃，以新会话重放同一 question；
    - 重放只发一次，仍失败则向上抛错（交给 chat 端点的 catch 块转 SSE error 事件）。
    """
    url = f"{endpoint}/api/v1/agent/stream"
    payload = {
        "question": question,
        "session_id": external_session_id,
        "mode": "auto",
    }

    async def _do_stream() -> AsyncIterator[tuple[str, str]]:
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT, trust_env=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                # 404 是"会话不存在"的明确信号，比 error 事件里的文案更确定，
                # 在进解析器之前先抛 ExternalSessionLostError 减少无用解析。
                if resp.status_code == 404:
                    raise ExternalSessionLostError(
                        f"外部 RAG 服务对 session {external_session_id} 返回 404"
                    )
                resp.raise_for_status()
                async for event_type, content in _parse_rag_standard_sse(resp):
                    yield event_type, content

    # 第 1 次尝试
    try:
        async for event_type, content in _do_stream():
            yield (event_type, content)
        return
    except ExternalSessionLostError as lost_exc:
        # 重建外部会话：用同一个 plat_{xxx} 前缀（按方案附录 A 隔离规则 1）。
        # title 暂用会话 id 的末 8 位即可，重建场景下不重要，新会话默认无 title。
        rebuilt_sid = await create_external_session(
            endpoint=endpoint,
            external_session_id=external_session_id,
            title="recovered",
            api_key=api_key,
            allowed_cidrs=allowed_cidrs,
        )
        logger.info(
            "rag_standard 外部会话失效已自动重建：%s（原因：%s）",
            rebuilt_sid, lost_exc,
        )
        # 把新映射回写到数据库（如果调用方提供了回调）。
        if on_session_reset is not None:
            result = on_session_reset(rebuilt_sid)
            if result is not None:
                await result
        # 用重建后的 sid 继续请求
        payload["session_id"] = rebuilt_sid
        async for event_type, content in _do_stream():
            yield (event_type, content)


async def _parse_rag_standard_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """解析 rag_standard SSE：每行 data: {json}，yield (event_type, content) 元组。

    协议要点（方案附录 A）：
      - 结束事件：data: {"type":"done","answer":"...完整文本","cancelled":false}
        解析到即停止（done 的完整 answer 作为持久化兜底）。
      - 错误事件：data: {"type":"error","error":"..."}
        若文案表明"会话失效"，抛 ExternalSessionLostError 触发自动重建；
        其他文案以 RuntimeError 抛出，交给 chat 端点 catch 块转 SSE error。
      - 增量事件：方案 v1.1 未指定增量格式，按"最常见的 RAG 协议实现"兼容下面三种：
          1) {"type":"delta","text":"..."}            ← 主流 RAG SSE 风格
          2) {"text":"..."} / {"content":"..."}       ← OpenAI 风格但无 type 字段
          3) data: 后直接是原始文本（无 JSON 包装）    ← 退化兜底
        解析器按顺序试探，能 decode 成 JSON 且其中含 text/content/type=='delta'
        之一则作为内容片段；否则把整段 data 当成裸文本片段。

    event_type 取值：
      ("message", text) — 正式回复片段
      ("done", answer)  — 收尾（解析完即停止，无需消费，返回前已 yield 完整 answer）
    错误不在此处 yield，由调用方决定如何转 SSE。
    """
    last_answer: str | None = None
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw:
            continue

        # data: 内部不是 JSON 的情况——按"裸文本片段"处理（增量事件格式猜测 #3）。
        try:
            event = json.loads(raw)
        except Exception:
            if raw:
                yield ("message", raw)
            continue

        if not isinstance(event, dict):
            # JSON 但不是 dict（如数组/字符串），仍按裸文本处理
            if raw:
                yield ("message", raw)
            continue

        event_type_value = event.get("type")
        if event_type_value == "done":
            # 收尾事件，answer 字段是完整回复；同时作为持久化兜底
            last_answer = event.get("answer")
            cancelled = bool(event.get("cancelled"))
            if last_answer and not cancelled:
                yield ("done", last_answer)
            return

        if event_type_value == "error":
            err_msg = event.get("error") or "外部 RAG 服务返回错误"
            if _is_session_lost_message(str(err_msg)):
                raise ExternalSessionLostError(
                    f"外部 RAG 报告会话失效：{err_msg}"
                )
            raise RuntimeError(str(err_msg))

        # 推理链路片段：{type:'thinking', text|content:'...'} → ("thinking", text)
        # 与 nap/openai_compatible 协议的 thinking 事件一致，由前端折叠展示。
        if event_type_value == "thinking":
            text = event.get("text") or event.get("content") or ""
            if text:
                yield ("thinking", text)
            continue

        # 增量事件：先看是否有 type=='delta' + text（猜测 #1）
        text = ""
        if event_type_value == "delta":
            text = event.get("text") or event.get("content") or ""
        elif event_type_value in (None, "message", "chunk"):
            # 猜测 #2：{type: 'message', text|content: '...'}，或压根没有 type 字段
            text = event.get("text") or event.get("content") or ""
        # 其他 type（如 tool_call / status 等）一律忽略
        if text:
            yield ("message", text)
