"""rag_standard 协议相关测试：解析器 / 会话 ID 生成 / 集成路径。

覆盖任务 #3 — 见 ee/docs/外部智能体一期方案.md §6.3
与附录 A（rag_standard 协议契约 + 6 条会话隔离规则）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.deps import async_session_factory
from app.core.security import encrypt_sensitive
from app.main import app
from app.models.external_agent import ExternalAgent
from app.models.external_agent_chat import ExternalAgentChatSession
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import external_agent_adapter
from app.services.external_agent_chat_service import create_session, delete_session
from tests.conftest import TestSessionLocal


# ── 纯单元测试：解析器与 ID 生成 ──────────────────────────────────────────────


def _lines_to_aiter(lines: list[str]):
    """把字符串列表喂给 _parse_rag_standard_sse（它要的是 httpx.Response.aiter_lines）。"""

    async def _gen():
        for line in lines:
            yield line

    return _gen()


class _FakeSseResponse:
    """给 _parse_rag_standard_sse 用的最小 httpx.Response 替身——只暴露 aiter_lines。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def aiter_lines(self):
        return _lines_to_aiter(self._lines)


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_handles_delta_events():
    """增量事件 {"type":"delta","text":"..."} 应被解析为 message 文本片段。"""
    resp = _FakeSseResponse([
        'data: {"type":"delta","text":"你好"}',
        'data: {"type":"delta","text":"，世界"}',
    ])
    chunks: list[str] = []
    async for event_type, content in external_agent_adapter._parse_rag_standard_sse(resp):
        if event_type == "message":
            chunks.append(content)
    assert "".join(chunks) == "你好，世界"


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_handles_raw_text_events():
    """data: 裸文本（不带 JSON 包装）按兜底当成 message 文本片段。"""
    resp = _FakeSseResponse([
        'data: 纯文本片段 A',
        'data: 纯文本片段 B',
    ])
    chunks: list[str] = []
    async for event_type, content in external_agent_adapter._parse_rag_standard_sse(resp):
        if event_type == "message":
            chunks.append(content)
    assert chunks == ["纯文本片段 A", "纯文本片段 B"]


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_handles_done_event():
    """{"type":"done","answer":"完整文本","cancelled":false} 解析为 done 事件并停止。"""
    resp = _FakeSseResponse([
        'data: {"type":"delta","text":"片段"}',
        'data: {"type":"done","answer":"完整答案","cancelled":false}',
        'data: {"type":"delta","text":"不应再被消费"}',
    ])
    events = [(t, c) async for t, c in external_agent_adapter._parse_rag_standard_sse(resp)]
    assert events == [("message", "片段"), ("done", "完整答案")]


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_handles_error_event():
    """{"type":"error","error":"..."} 抛 RuntimeError，调用方捕后转 SSE 错误事件。"""
    resp = _FakeSseResponse([
        'data: {"type":"error","error":"upstream crashed"}',
    ])
    with pytest.raises(RuntimeError, match="upstream crashed"):
        async for _ in external_agent_adapter._parse_rag_standard_sse(resp):
            pass


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_done_with_cancelled_does_not_yield_answer():
    """done.cancelled=true 时不把 answer 当最终回复 yield（避免给取消的回答做持久化）。"""
    resp = _FakeSseResponse([
        'data: {"type":"done","answer":"被取消的文本","cancelled":true}',
    ])
    events = [(t, c) async for t, c in external_agent_adapter._parse_rag_standard_sse(resp)]
    # 收尾不产任何事件（调用方拿到 done 即停止解析）
    assert events == []


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_session_lost_raises_typed_exception():
    """{"type":"error","error":"session not found"} 应抛 ExternalSessionLostError。"""
    resp = _FakeSseResponse([
        'data: {"type":"error","error":"session not found"}',
    ])
    with pytest.raises(external_agent_adapter.ExternalSessionLostError):
        async for _ in external_agent_adapter._parse_rag_standard_sse(resp):
            pass


@pytest.mark.asyncio
async def test_parse_rag_standard_sse_handles_text_only_json():
    """{"text":"..."}（无 type 字段）按兜底解析为 message。"""
    resp = _FakeSseResponse([
        'data: {"text":"片段-A"}',
        'data: {"content":"片段-B"}',
    ])
    chunks: list[str] = []
    async for event_type, content in external_agent_adapter._parse_rag_standard_sse(resp):
        if event_type == "message":
            chunks.append(content)
    assert chunks == ["片段-A", "片段-B"]


def test_compute_platform_external_session_id_uses_plat_prefix():
    """plat_ 前缀是隔离规则 1 的硬约束，绝不复用外部文档示例的固定值（如 default）。"""
    plat_id = str(uuid.uuid4())
    sid = external_agent_adapter.compute_platform_external_session_id(plat_id)
    assert sid == f"plat_{plat_id}"
    assert sid.startswith("plat_")
    assert "default" not in sid


def test_compute_platform_external_session_id_never_returns_default():
    """任何输入下都不得返回外部文档示例值 "default"（防跨用户串扰）。"""
    for _ in range(20):
        plat_id = str(uuid.uuid4())
        sid = external_agent_adapter.compute_platform_external_session_id(plat_id)
        assert sid != "default"
        assert not sid.endswith("_default")


# ── 集成测试：外部会话生命周期（mock httpx transport）─────────────────────────


class _RagMockTransport(httpx.AsyncBaseTransport):
    """针对 rag_standard 端点的最小 MockTransport：按 URL 分发到注册的 handler。"""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict]] = []  # (method, url, json_body)

    def handler(self, matcher: str):
        """装饰器：注册一个 matcher（url substring）对应的 async handler。"""
        def decorator(fn):
            self.handlers[matcher] = fn
            return fn
        return decorator

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # httpx 0.28+ 的 request.json()/content 是 lazy 的，需先 aread()
        try:
            content = await request.aread()
            body = json.loads(content) if content else {}
        except Exception:
            body = {}
        self.calls.append((request.method, str(request.url), body))

        for matcher, fn in self.handlers.items():
            if matcher in str(request.url):
                return await fn(request, body)

        return httpx.Response(404, json={"error": "no mock handler"})


@pytest.fixture
def rag_mock(monkeypatch):
    """替换 external_agent_adapter 内的 httpx.AsyncClient 为使用 MockTransport 的实例。"""
    transport = _RagMockTransport()

    orig_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        # kwargs.trust_env 仍由调用方控制；只注入 transport
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(external_agent_adapter.httpx, "AsyncClient", _factory)

    # 直连环境（避免被 _resolve_wsl_endpoint 改写）
    monkeypatch.setattr(external_agent_adapter, "_resolve_wsl_endpoint", lambda x: x)
    return transport


# ── verify_connection 用例 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_connection_rag_standard_200_returns_true(rag_mock):
    """GET /api/v1/agent/sessions?limit=1 → 200 视为可达。"""
    @rag_mock.handler("/api/v1/agent/sessions")
    async def _ok(request, body):
        return httpx.Response(200, json=[{"session_id": "plat_xxx"}])

    ok = await external_agent_adapter.verify_connection(
        endpoint="http://rag.test", api_key=None, protocol="rag_standard",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_connection_rag_standard_503_returns_false(rag_mock):
    """HTTP 503 视为不可达（不允许 5xx 当成功，与其它协议判定一致）。"""
    @rag_mock.handler("/api/v1/agent/sessions")
    async def _down(request, body):
        return httpx.Response(503, json={})

    ok = await external_agent_adapter.verify_connection(
        endpoint="http://rag.test", api_key=None, protocol="rag_standard",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_verify_connection_rag_standard_404_returns_false(rag_mock):
    """HTTP 404 视为不可达。"""
    @rag_mock.handler("/api/v1/agent/sessions")
    async def _down(request, body):
        return httpx.Response(404, json={})

    ok = await external_agent_adapter.verify_connection(
        endpoint="http://rag.test", api_key=None, protocol="rag_standard",
    )
    assert ok is False


# ── create_session / delete_session 集成 ────────────────────────────────────


async def _make_org_agent(protocol: str, session_managed_by: str | None) -> tuple[User, Organization, ExternalAgent]:
    """建一个最小可用组织 + 用户 + Agent；不含加密 api_key 时 encryption 可空。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"rag-org-{suffix}", slug=f"rag-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"rag-{suffix}@example.com", name=f"rag-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.admin))
        agent = ExternalAgent(
            org_id=org.id,
            name=f"rag-agent-{suffix}",
            endpoint="http://rag.test",
            protocol=protocol,
            session_managed_by=session_managed_by,
            api_key_encrypted=encrypt_sensitive("dummy-key"),
        )
        db.add(agent)
        await db.commit()
        await db.refresh(user)
        await db.refresh(agent)
        return user, org, agent


@pytest.mark.asyncio
async def test_create_session_calls_external_sessions_endpoint_and_persists_mapping(rag_mock):
    """External session 映射：create_session 应调外部 POST /api/v1/agent/sessions，
    并把返回的 session_id 写入 ExternalAgentChatSession.external_session_id。"""
    captured: dict = {}

    @rag_mock.handler("/api/v1/agent/sessions")
    async def _create(request, body):
        captured["body"] = body
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"session_id": body["session_id"], "title": body["title"]},
        )

    _, _, agent = await _make_org_agent(
        protocol="rag_standard", session_managed_by="external",
    )

    user, org, _ = await _make_org_agent(
        protocol="rag_standard", session_managed_by="external",
    )
    # 用第二个 user/org 但绑定第一个 agent_id（agent 同 org；保证 org 一致）
    async with TestSessionLocal() as db:
        # 把第一个 agent 改成 org_id 与 user 相同，便于创会话
        result = await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent.id)
        )
        a = result.scalar_one()
        a.org_id = org.id
        await db.commit()
        await db.refresh(a)

    async with TestSessionLocal() as db:
        result = await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent.id)
        )
        agent_orm = result.scalar_one()
        session = await create_session(
            agent_id=agent.id, org_id=org.id, user_id=str(user.id), db=db,
        )

    # 平台会话的 external_session_id 必须是 plat_{id} 格式，由外部接口"原样回传"
    assert session.external_session_id is not None
    assert session.external_session_id.startswith("plat_")
    assert session.external_session_id == f"plat_{session.id}"

    # 外部 POST /sessions 确实被调过一次
    create_calls = [c for c in rag_mock.calls if c[1].endswith("/api/v1/agent/sessions")]
    assert len(create_calls) == 1
    method, url, body = create_calls[0]
    assert method == "POST"
    assert body["session_id"] == f"plat_{session.id}"
    # api_key 走 Authorization 头
    assert captured["headers"].get("authorization") == "Bearer dummy-key"


@pytest.mark.asyncio
async def test_create_session_skips_external_call_when_not_external_managed(rag_mock):
    """session_managed_by != "external" 时跳过外部映射，external_session_id 保持 NULL。"""
    @rag_mock.handler("/api/v1/agent/sessions")
    async def _should_not_call(request, body):
        raise AssertionError("external /sessions 不应被调用")

    user, org, agent = await _make_org_agent(
        protocol="rag_standard", session_managed_by=None,
    )

    async with TestSessionLocal() as db:
        session = await create_session(
            agent_id=agent.id, org_id=org.id, user_id=str(user.id), db=db,
        )
        assert session.external_session_id is None
        assert rag_mock.calls == []


@pytest.mark.asyncio
async def test_create_session_skips_external_call_for_non_rag_standard(rag_mock):
    """非 rag_standard 协议不调外部 /sessions（openai/nap/custom 各自有自己的会话机制）。"""
    @rag_mock.handler("/api/v1/agent/sessions")
    async def _should_not_call(request, body):
        raise AssertionError("external /sessions 不应被调用")

    user, org, agent = await _make_org_agent(
        protocol="openai_compatible", session_managed_by=None,
    )

    async with TestSessionLocal() as db:
        session = await create_session(
            agent_id=agent.id, org_id=org.id, user_id=str(user.id), db=db,
        )
        assert session.external_session_id is None
        assert rag_mock.calls == []


@pytest.mark.asyncio
async def test_delete_session_calls_external_history_endpoint(rag_mock):
    """软删除时若 mapping 非空且为 rag_standard + external，则额外调 DELETE /history/{sid}。"""
    @rag_mock.handler("/api/v1/agent/history/")
    async def _del(request, body):
        return httpx.Response(200, json={"message": "deleted"})

    user, org, agent = await _make_org_agent(
        protocol="rag_standard", session_managed_by="external",
    )

    # 直接造一个已有 mapping 的平台会话（绕过 create_session 的外部调用）
    plat_session = ExternalAgentChatSession(
        agent_id=agent.id, org_id=org.id, user_id=user.id,
        external_session_id="plat_test_mapping",
    )
    async with TestSessionLocal() as db:
        db.add(plat_session)
        await db.commit()
        await db.refresh(plat_session)
        sid_before_delete = plat_session.id
    async with TestSessionLocal() as db:
        await delete_session(
            session_id=str(sid_before_delete), user_id=str(user.id),
            agent_id=agent.id, db=db,
        )

    delete_calls = [c for c in rag_mock.calls if "/api/v1/agent/history/" in c[1]]
    assert len(delete_calls) == 1
    method, url, _ = delete_calls[0]
    assert method == "DELETE"
    assert url.endswith("/api/v1/agent/history/plat_test_mapping")


@pytest.mark.asyncio
async def test_delete_session_continues_when_external_history_fails(rag_mock):
    """外部历史删除失败不应阻塞本地软删除（用户的本意是删除本地会话）。"""
    @rag_mock.handler("/api/v1/agent/history/")
    async def _fail(request, body):
        return httpx.Response(500, json={"error": "boom"})

    user, org, agent = await _make_org_agent(
        protocol="rag_standard", session_managed_by="external",
    )
    plat_session = ExternalAgentChatSession(
        agent_id=agent.id, org_id=org.id, user_id=user.id,
        external_session_id="plat_to_be_cleaned",
    )
    async with TestSessionLocal() as db:
        db.add(plat_session)
        await db.commit()
        await db.refresh(plat_session)
        sid = plat_session.id
    async with TestSessionLocal() as db:
        # 不应抛错
        await delete_session(
            session_id=str(sid), user_id=str(user.id),
            agent_id=agent.id, db=db,
        )

    # 本地软删除仍然完成
    async with TestSessionLocal() as db:
        result = await db.execute(
            select(ExternalAgentChatSession).where(
                ExternalAgentChatSession.id == sid,
            )
        )
        row = result.scalar_one()
        from app.models.base import not_deleted
        still_visible = await db.execute(
            select(ExternalAgentChatSession).where(
                ExternalAgentChatSession.id == sid,
                not_deleted(ExternalAgentChatSession),
            )
        )
        assert still_visible.scalar_one_or_none() is None


# ── chat_stream 集成：会话失效自动重建 + 重放 ────────────────────────────────


def _sse_lines(events: list[dict | str]) -> str:
    """把事件列表（dict 或裸字符串）序列化为 SSE 文本块。"""
    out = []
    for ev in events:
        if isinstance(ev, str):
            out.append(f"data: {ev}")
        else:
            out.append(f"data: {json.dumps(ev, ensure_ascii=False)}")
    return "\n\n".join(out) + "\n\n"


@pytest.mark.asyncio
async def test_chat_stream_rag_standard_yields_chunks_and_done(rag_mock):
    """第 1 次尝试：正常流（delta + done），应原样产出消息片段与 done 事件。"""
    @rag_mock.handler("/api/v1/agent/stream")
    async def _stream(request, body):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_lines([
                {"type": "delta", "text": "片段-1"},
                {"type": "delta", "text": "片段-2"},
                {"type": "done", "answer": "完整答案", "cancelled": False},
            ]),
        )

    events = []
    async for et, content in external_agent_adapter.chat_stream(
        endpoint="http://rag.test",
        api_key=None,
        protocol="rag_standard",
        messages=[{"role": "user", "content": "hi"}],
        session_id="abc",
    ):
        events.append((et, content))
    assert ("message", "片段-1") in events
    assert ("message", "片段-2") in events
    assert ("done", "完整答案") in events


@pytest.mark.asyncio
async def test_chat_stream_rag_standard_auto_recovers_from_session_lost(rag_mock):
    """第 1 次请求服务端说"会话不存在"，适配器应自动重建外部会话、重放问题、回写映射。"""
    attempt = {"n": 0}
    callback_received: list[str] = []

    async def _on_reset(new_sid: str) -> None:
        callback_received.append(new_sid)

    @rag_mock.handler("/api/v1/agent/sessions")
    async def _create(request, body):
        # 重建外部会话
        return httpx.Response(
            200, json={"session_id": body["session_id"], "ok": True},
        )

    @rag_mock.handler("/api/v1/agent/stream")
    async def _stream(request, body):
        attempt["n"] += 1
        # 第 1 次：返回 session not found，触发重建
        if attempt["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_lines([
                    {"type": "error", "error": "session not found"},
                ]),
            )
        # 第 2 次（重放）：正常流式响应
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_lines([
                {"type": "delta", "text": "重放后的答案"},
                {"type": "done", "answer": "重放后的答案", "cancelled": False},
            ]),
        )

    events = []
    async for et, content in external_agent_adapter.chat_stream(
        endpoint="http://rag.test",
        api_key=None,
        protocol="rag_standard",
        messages=[{"role": "user", "content": "hi"}],
        session_id="plat_xyz",
        external_session_id="plat_xyz",
        on_external_session_reset=_on_reset,
    ):
        events.append((et, content))

    # 调试日志里能看到重建被触发
    assert attempt["n"] == 2
    # 回调被调用一次
    assert callback_received == ["plat_xyz"]
    # 用户看到的是重放后的内容（不含第一次的错误产物）
    messages = [c for et, c in events if et == "message"]
    done_text = next((c for et, c in events if et == "done"), None)
    assert "重放后的答案" in messages or done_text == "重放后的答案"
    # 错误事件不应外泄为流片段
    assert not any("session not found" in (c or "") for et, c in events if et == "message")


@pytest.mark.asyncio
async def test_chat_stream_rag_standard_replay_failure_surfaces_error(rag_mock):
    """首次会话失效，重放仍失败时适配器向上抛错，由上层 catch 转 SSE error。"""
    attempt = {"n": 0}

    @rag_mock.handler("/api/v1/agent/sessions")
    async def _create(request, body):
        # 重建 OK
        return httpx.Response(200, json={"session_id": body["session_id"]})

    @rag_mock.handler("/api/v1/agent/stream")
    async def _stream(request, body):
        attempt["n"] += 1
        if attempt["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_lines([{"type": "error", "error": "session not found"}]),
            )
        # 重放后仍报错（极端边界）
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_lines([{"type": "error", "error": "service crashed"}]),
        )

    raised: list[Exception] = []
    try:
        async for _ in external_agent_adapter.chat_stream(
            endpoint="http://rag.test",
            api_key=None,
            protocol="rag_standard",
            messages=[{"role": "user", "content": "hi"}],
            session_id="plat_q",
            external_session_id="plat_q",
        ):
            pass
    except RuntimeError as exc:
        raised.append(exc)
    assert len(raised) == 1
    assert "service crashed" in str(raised[0])
    assert attempt["n"] == 2
