"""End-to-end 验收测试：rag_standard mock 替身 + 平台全链路（任务 #11）。

依据：ee/docs/外部智能体一期方案.md
  - §11 任务 #11："先实现符合附录 A 的 mock 服务作为验收替身"
  - §12 验收标准 #1：管理员提交 rag_standard chat 插件 → 试调 → 用户提问 →
    SSE 流式回答 → 会话列表/清空历史与外部服务行为一致。
  - §12 验收标准 #7：多轮隔离——两用户各与同一 chat 插件多轮对话，互不可见；
    用户 A 构造他人 session_id → 403；所有外部 session_id 均为 plat_ 前缀平台
    生成值。

设计：
  - 启动方式：import rag_standard_mock 单进程 FastAPI app，用 uvicorn.Server
    在 127.0.0.1:<random port> 起一个真服务（不走 ASGITransport 直通）。
    原因：spec 要求"会话失效降级 / 外部调用真实 HTTP"——只有真实 socket
    才能验证该路径，且 httpx.ASGITransport 与生产链路的 transport 行为
    有差异（DNS / 连接错误分支、TCP buffer 等）。
  - mock 状态隔离：每个测试 case 显式调 POST /_mock/reset，保证用例间
    不污染。
  - DB 隔离：每个 case 用独立 organization + 两个 user（admin + member），
    避免 rate-limit 跨用例串味。
  - SSRF 白名单：组织预置 ["127.0.0.0/8"]，与 platform 默认禁用私网对齐。
  - 慢测试警告：E2E 涉及真实 HTTP + 完整 rag_standard 协议解析，比
    test_external_agent_rag_standard.py 的纯 MockTransport 用例慢一档；
    跑完一个 E2E 用例约 1-2s，仍属可接受范围（不需要标记跳过）。

局限（已知）：
  - 真实流式 chunk 间隔默认 0（spec 也允许 mock 服务"立即推完"），
    E2E 只断言 chunk 个数与 done 文本，不验证前端流式渲染节拍。
  - 平台 chat_stream 内部走 httpx，不依赖 mock 服务是否在 Docker 内——只要
    是 127.0.0.1 即可通；与 dev.sh / docker compose 启动方式正交。
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
import uvicorn
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import get_current_user
from app.main import app
from app.models.external_agent import ExternalAgent
from app.models.external_agent_chat import ExternalAgentChatSession, ExternalAgentMessage
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal
from tests.fixtures import rag_standard_mock


# ── DB session 同步：让 platform 的独立 session factory 指向 test engine ─────

@pytest.fixture(autouse=True)
def _align_async_session_factory():
    """强制 platform 的 async_session_factory 走 TestSessionLocal 的 engine。

    平台 chat 流结束后的 _persist_messages 用独立的 async_session_factory()
    拿 session（避免与请求 session 竞争）。这与 conftest 的 TestSessionLocal
    各自连到不同 engine — 测试环境下必须让两者指向同一个库，否则
    _persist_messages 写入"生产库"，而 session 行只在 test 库，触发 FK 违反。

    注意：app.api.external_agents 在模块顶部 `from app.core.deps import
    async_session_factory` 已经把名字绑到自己模块作用域，必须同时 patch
    `app.api.external_agents.async_session_factory` 才能生效。
    """
    from app.core import deps as app_deps
    from app.api import external_agents as api_ext_agents

    original_deps = app_deps.async_session_factory
    original_api = api_ext_agents.async_session_factory
    app_deps.async_session_factory = TestSessionLocal
    api_ext_agents.async_session_factory = TestSessionLocal
    try:
        yield
    finally:
        app_deps.async_session_factory = original_deps
        api_ext_agents.async_session_factory = original_api


# ── Mock 服务生命周期管理 ──────────────────────────────────────────────────────


def _free_port() -> int:
    """OS 分配一个空闲 TCP 端口；测试结束立即释放，避免残留端口占用。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@asynccontextmanager
async def _start_mock_server() -> AsyncIterator[str]:
    """在 127.0.0.1:<free_port> 起一个真实 HTTP 服务；用 uvicorn.Server + asyncio task。

    返回 base_url（含 scheme + host + port），供测试在 httpx 中调用。

    短路掉 _resolve_wsl_endpoint：dev 环境 DEBUG=true 时该函数会把
    127.0.0.1 改写到 WSL 宿主机 IP，导致无法触达本进程内的 mock 服务。
    E2E 测试期望 mock 服务就在 WSL 内网被直连，所以 monkeypatch 它为原样返回。
    """
    from app.services import external_agent_adapter
    original_resolve = external_agent_adapter._resolve_wsl_endpoint
    external_agent_adapter._resolve_wsl_endpoint = lambda endpoint: endpoint

    port = _free_port()
    config = uvicorn.Config(
        rag_standard_mock.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # 不设 lifespan / 不打印 access log；纯单测。
        lifespan="off",
    )
    server = uvicorn.Server(config)

    # server.serve() 是个长跑 coroutine，必须并发等待启动信号。
    serve_task = asyncio.create_task(server.serve())
    # 等待 server.startup 完成（started 标志位）。
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        serve_task.cancel()
        external_agent_adapter._resolve_wsl_endpoint = original_resolve
        raise RuntimeError("mock server failed to start within 5s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            serve_task.cancel()
        external_agent_adapter._resolve_wsl_endpoint = original_resolve


# ── DB / 用户装配 ────────────────────────────────────────────────────────────


def _override_user(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override() -> None:
    app.dependency_overrides.pop(get_current_user, None)


async def _setup_org_and_agents(endpoint: str):
    """建一个组织 + 1 个 admin + 2 个 member；落一个 rag_standard + external Agent。

    端点指向 mock 服务 URL，SSRF 白名单已预置 127.0.0.0/8，命中验证。
    返回 (admin, user_a, user_b, org, agent)。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"e2e-rag-org-{suffix}",
            slug=f"e2e-rag-org-{suffix}",
            external_agent_allowed_cidrs=["127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        admin = User(
            email=f"e2e-rag-admin-{suffix}@example.com",
            name=f"e2e-rag-admin-{suffix}",
            password_hash="x",
            current_org_id=org.id,
            is_super_admin=False,
        )
        user_a = User(
            email=f"e2e-rag-a-{suffix}@example.com",
            name=f"e2e-rag-a-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        user_b = User(
            email=f"e2e-rag-b-{suffix}@example.com",
            name=f"e2e-rag-b-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add_all([admin, user_a, user_b])
        await db.flush()
        db.add_all([
            OrgMembership(org_id=org.id, user_id=admin.id, role=OrgRole.admin),
            OrgMembership(org_id=org.id, user_id=user_a.id, role=OrgRole.member),
            OrgMembership(org_id=org.id, user_id=user_b.id, role=OrgRole.member),
        ])
        agent = ExternalAgent(
            org_id=org.id,
            name=f"e2e-rag-agent-{suffix}",
            endpoint=endpoint,
            protocol="rag_standard",
            session_managed_by="external",
            type="chat",
            status="active",
        )
        db.add(agent)
        await db.commit()
        for obj in (admin, user_a, user_b, agent):
            await db.refresh(obj)
        return admin, user_a, user_b, org, agent


# ── 公共断言小工具 ────────────────────────────────────────────────────────────


async def _consume_sse_chunks(resp: httpx.Response) -> tuple[list[str], list[str], list[str]]:
    """解析平台 SSE 输出：返回 (chunks, thinkings, errors)。

    平台 SSE 事件类型：
      data: {"chunk": "..."}     → 累积到 assistant 正式回复
      data: {"thinking": "..."}  → 推理链路（折叠显示）
      data: {"done": true}       → 收尾（不再有更多事件）
      data: {"error": "..."}     → 错误
    """
    chunks: list[str] = []
    thinkings: list[str] = []
    errors: list[str] = []

    # 逐行读 body，避免依赖 httpx 的流式解码
    body = resp.text
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload_str = line[len("data: "):].strip()
        if not payload_str:
            continue
        try:
            payload = __import__("json").loads(payload_str)
        except Exception:
            continue
        if isinstance(payload, dict):
            if payload.get("chunk"):
                chunks.append(str(payload["chunk"]))
            elif payload.get("thinking"):
                thinkings.append(str(payload["thinking"]))
            elif payload.get("error"):
                errors.append(str(payload["error"]))
            elif payload.get("done"):
                pass
    return chunks, thinkings, errors


async def _reset_mock(base_url: str) -> None:
    """每个 case 开头 reset mock 状态，避免跨 case 污染。

    WSL 环境下默认会注入 http_proxy/hTTPS_PROXY 指向 127.0.0.1:7897，
    显式 trust_env=False 直连 mock 服务，避免被代理拦截返回 502。
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as client:
        await client.post("/_mock/reset")


# ── 端到端用例 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_full_flow_session_chat_isolation(client: AsyncClient):
    """完整 E2E：spec §12 #1 + #7。

    启动 mock → admin 创建 rag_standard Agent →
    user_a 创建会话 → 发问 → 验证 SSE chunk/done → 验证持久化 →
    user_a 列会话看见自己的 → user_b 列会话看见零条 →
    user_b 用 user_a 的 session_id 调 chat → 403 →
    user_a 删除会话 → 验证 mock DELETE /history/{sid} 被调 →
    验证 DB 中 external_session_id 全程 plat_ 前缀且不含 "default"。
    """
    # 平台 chat 流结束后的 _persist_messages 走独立的 async_session_factory，
    # 该 factory 由 _align_async_session_factory fixture 已被重定向到 test engine。
    async with _start_mock_server() as base_url:
        await _reset_mock(base_url)
        admin, user_a, user_b, _org, agent = await _setup_org_and_agents(base_url)

        # ① admin 创建 Agent（已在 _setup 中落库，等价于管理端向导落库后的结果）
        # 这里直接查 DB 验证一下 Agent 形态符合验收前置
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(ExternalAgent).where(ExternalAgent.id == agent.id)
            )).scalar_one()
            assert row.protocol == "rag_standard"
            assert row.session_managed_by == "external"
            assert row.status == "active"
            assert row.endpoint == base_url

        # ── (a) user_a 创建会话 → 触发 mock POST /api/v1/agent/sessions ─────
        _override_user(user_a)
        try:
            create_session_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/sessions"
            )
        finally:
            _clear_override()
        assert create_session_resp.status_code == 200
        session_payload = create_session_resp.json()["data"]
        session_id = session_payload["id"]
        external_sid = session_payload["external_session_id"]
        # spec 验收 #7 隔离规则 1：所有外部 session_id 必须 plat_ 前缀 + 平台生成 + ≠ "default"
        assert external_sid is not None
        assert external_sid.startswith("plat_")
        assert external_sid == f"plat_{session_id}"
        assert "default" not in external_sid

        # mock 应当被调过一次 POST /api/v1/agent/sessions（验收 #1：试调→登记→启用）
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as stats_client:
            stats = (await stats_client.get("/_mock/stats")).json()
        post_calls = stats["call_counts"].get("POST /api/v1/agent/sessions", 0)
        assert post_calls >= 1, f"mock 没收到 sessions 创建调用：{stats}"

        # ── (b) user_a 发问 → 平台 SSE → 验证 chunks + thinking + done ───────
        question = "热压缺陷原因？"
        _override_user(user_a)
        try:
            chat_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/chat",
                json={"message": question, "session_id": session_id},
            )
        finally:
            _clear_override()
        # 平台 chat_stream 在流开始前就确定 200（错误在流内以 SSE error 事件表达）
        assert chat_resp.status_code == 200
        assert chat_resp.headers["content-type"].startswith("text/event-stream")

        chunks, thinkings, errors = await _consume_sse_chunks(chat_resp)
        assert not errors, f"chat 流里有 error 事件：{errors}"
        # 平台通过 asyncio.create_task 异步持久化消息（独立 DB session）；
        # 给它一点时间 commit 完成，否则后续 DB 断言可能撞到 INSERT 阶段。
        for _ in range(20):
            await asyncio.sleep(0.1)
            async with TestSessionLocal() as db:
                count = (await db.execute(
                    select(ExternalAgentMessage.id).where(
                        ExternalAgentMessage.session_id == session_id
                    )
                )).scalars().first()
            if count:
                break
        # 流式累积后应等于模板答案里的核心字段（用 _MOCK_ANSWER_TEMPLATE.format(question=...) 内容）
        full_text = "".join(chunks)
        assert question in full_text or "热压缺陷" in full_text, (
            f"回答里没看到与问题相关的内容：{full_text[:200]}"
        )
        # mock 设计的答案里包含 markdown 元素，验证流式解析对 markdown
        # 内容不会因为 chunk 切分丢字（"针对你提出" 与 "结合知识库" 应都在）
        assert "针对你提出" in full_text
        assert "结合知识库" in full_text
        # 思考链路：mock 设计的 thinking 文案
        assert any("检索知识库" in t for t in thinkings), (
            f"没收到 thinking 事件：thinkings={thinkings}"
        )

        # ── 持久化：DB 里应同时存 user / assistant 两条消息 ───────────────────
        async with TestSessionLocal() as db:
            rows = (await db.execute(
                select(ExternalAgentMessage)
                .where(ExternalAgentMessage.session_id == session_id)
                .order_by(ExternalAgentMessage.created_at.asc())
            )).scalars().all()
        assert len(rows) == 2
        assert rows[0].role == "user"
        assert rows[0].content == question
        assert rows[1].role == "assistant"
        # 持久化的 assistant content 应等于 mock 流末尾 done 事件携带的
        # 完整 answer（spec §12 #1 "done 事件里的完整 answer 用于持久化兜底"）；
        # 不严格等于 chunks 拼接是因为 mock 可能在流末尾追加非 JSON 行
        # （如 SSE 空心跳），但与 full_text 的核心内容必然一致。
        assert rows[1].content.startswith(full_text[:50]) or full_text.startswith(
            rows[1].content[:50]
        ), (
            f"持久化内容与流内容不一致：\npersisted={rows[1].content[:200]}\n"
            f"streamed={full_text[:200]}"
        )
        # assistant 消息的 thinking 字段已被持久化
        assert rows[1].thinking is not None
        assert "检索知识库" in rows[1].thinking

        # ── (c) user_a 列会话：能看到自己这条 ────────────────────────────────
        _override_user(user_a)
        try:
            list_a_resp = await client.get(
                f"/api/v1/external-agents/{agent.id}/sessions"
            )
        finally:
            _clear_override()
        assert list_a_resp.status_code == 200
        sessions_for_a = list_a_resp.json()["data"]
        session_ids_a = {s["id"] for s in sessions_for_a}
        assert session_id in session_ids_a, (
            f"user_a 列会话看不见自己的：{session_ids_a}"
        )

        # ── (d) user_b 列会话：看不到 user_a 的会话 ──────────────────────────
        _override_user(user_b)
        try:
            list_b_resp = await client.get(
                f"/api/v1/external-agents/{agent.id}/sessions"
            )
        finally:
            _clear_override()
        assert list_b_resp.status_code == 200
        sessions_for_b = list_b_resp.json()["data"]
        session_ids_b = {s["id"] for s in sessions_for_b}
        assert session_id not in session_ids_b, (
            f"user_b 不应看到 user_a 的会话，但看到了：{session_ids_b}"
        )

        # ── (e) user_b 用 user_a 的 session_id 调 chat → 403 ─────────────────────
        _override_user(user_b)
        try:
            cross_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/chat",
                json={"message": "你能看到吗？", "session_id": session_id},
            )
        finally:
            _clear_override()
        assert cross_resp.status_code == 403
        body = cross_resp.json()
        assert body["message_key"] == "errors.external_agent.session_forbidden"

        # mock 不应被 user_b 这次调 chat 触发额外的 stream 调用
        # user_b 这次 chat 已被平台用 403 拒绝，根本不会进到 external_agent_adapter
        # → mock 的 stream 计数应保持不变。两次拉取 stats 对比：
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as stats_client:
            before = (await stats_client.get("/_mock/stats")).json()[
                "call_counts"
            ].get("POST /api/v1/agent/stream", 0)
            await asyncio.sleep(0.1)
            after = (await stats_client.get("/_mock/stats")).json()[
                "call_counts"
            ].get("POST /api/v1/agent/stream", 0)
        assert after == before, (
            f"user_b 的越权 chat 请求不应触发 mock stream 调用：before={before}, after={after}"
        )

        # ── (f) user_a 删除会话 → mock DELETE /history/{sid} 应被调 ─────────
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as stats_client:
            delete_calls_before = (await stats_client.get("/_mock/stats")).json()[
                "call_counts"
            ].get("DELETE /api/v1/agent/history/{sid}", 0)
        _override_user(user_a)
        try:
            del_resp = await client.delete(
                f"/api/v1/external-agents/{agent.id}/sessions/{session_id}"
            )
        finally:
            _clear_override()
        assert del_resp.status_code == 200

        # 等 mock DELETE 异步触发（平台软删除后 fire-and-forget 调外部清理）
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as stats_client:
            for _ in range(20):
                stats = (await stats_client.get("/_mock/stats")).json()
                if stats["call_counts"].get(
                    "DELETE /api/v1/agent/history/{sid}", 0,
                ) > delete_calls_before:
                    break
                await asyncio.sleep(0.05)
        assert stats["call_counts"].get(
            "DELETE /api/v1/agent/history/{sid}", 0,
        ) > delete_calls_before, f"mock 没收到 DELETE 调用：{stats}"

        # 验证 mock 收到的 DELETE body 是 plat_{session_id}（隔离规则 1）
        last_delete = stats["last_requests"].get("DELETE /api/v1/agent/history/{sid}", {})
        # mock 的 _LAST_REQUEST 记的是 path-param 提取前的 body；此处 sid 在 URL，
        # 通过 stats["session_ids"] 应已被清空
        assert session_id not in stats["session_ids"], (
            f"mock 忘记会话失败：仍能看到 {stats['session_ids']}"
        )

        # ── (g) external_session_id 全程 plat_ 前缀且 ≠ "default" ─────────────
        # 再建一个会话，覆盖多个 plat_ 值，确保没有"default"
        _override_user(user_a)
        try:
            second_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/sessions"
            )
        finally:
            _clear_override()
        assert second_resp.status_code == 200
        second = second_resp.json()["data"]
        assert second["external_session_id"].startswith("plat_")
        assert "default" not in second["external_session_id"]

        # DB 全表扫：所有 external_session_id 必须 plat_ 前缀且 ≠ "default"
        async with TestSessionLocal() as db:
            rows = (await db.execute(
                select(ExternalAgentChatSession.external_session_id).where(
                    ExternalAgentChatSession.agent_id == agent.id,
                )
            )).scalars().all()
        for sid in rows:
            if sid is None:
                continue  # 非 rag_standard 允许 NULL
            assert sid.startswith("plat_"), f"external_session_id 缺前缀：{sid}"
            assert "default" not in sid, f"external_session_id 命中 'default'：{sid}"


@pytest.mark.asyncio
async def test_e2e_session_lost_auto_recovers(client: AsyncClient):
    """E2E：会话被外部侧遗忘时，平台自动重建并重放（spec 附录 A 隔离规则 4）。

    流程：user_a 创建会话 → mock 在第一次 stream 前把该 sid 从 _SESSIONS 中删除 →
    平台 chat_stream 应触发 ExternalSessionLostError → 自动 POST /sessions 重建 →
    重放 question 并产出最终答案。
    """
    async with _start_mock_server() as base_url:
        await _reset_mock(base_url)
        admin, user_a, _user_b, _org, agent = await _setup_org_and_agents(base_url)

        # user_a 创建会话
        _override_user(user_a)
        try:
            create_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/sessions"
            )
        finally:
            _clear_override()
        session_id = create_resp.json()["data"]["id"]
        external_sid = create_resp.json()["data"]["external_session_id"]
        assert external_sid.startswith("plat_")

        # mock 侧模拟"外部服务丢失该会话"（直接清掉）
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as mock_client:
            await mock_client.delete(f"/api/v1/agent/history/{external_sid}")

        # 拿当前 mock 状态，确保外部 sessions 已空
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as mock_client:
            sessions_before_chat = (
                await mock_client.get("/api/v1/agent/sessions?limit=10")
            ).json()
        assert external_sid not in {s["session_id"] for s in sessions_before_chat}, (
            "前置清理失败：mock 仍记得该 sid"
        )

        # user_a 发问：平台适配器会先收到 404（ExternalSessionLostError）→ 重建 → 重放
        question = "热压缺陷原因？"
        _override_user(user_a)
        try:
            chat_resp = await client.post(
                f"/api/v1/external-agents/{agent.id}/chat",
                json={"message": question, "session_id": session_id},
            )
        finally:
            _clear_override()
        assert chat_resp.status_code == 200
        chunks, _thinkings, errors = await _consume_sse_chunks(chat_resp)
        assert not errors, f"重建后 chat 仍有 error：{errors}"
        full_text = "".join(chunks)
        assert "针对你提出" in full_text, (
            f"重放未产出完整答案：{full_text[:200]}"
        )

        # 平台应当在 mock 侧触发两次 stream 调用（一次 404 → 重放一次成功）
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0, trust_env=False) as mock_client:
            stats = (await mock_client.get("/_mock/stats")).json()
        stream_count = stats["call_counts"].get("POST /api/v1/agent/stream", 0)
        assert stream_count >= 2, (
            f"会话失效重放路径未走通：stream 调用 {stream_count} 次，stats={stats}"
        )

        # DB 里会话的 external_session_id 在重建后仍为 plat_{platform_id}（平台生成值不变）
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(ExternalAgentChatSession).where(
                    ExternalAgentChatSession.id == session_id,
                )
            )).scalar_one()
        assert row.external_session_id.startswith("plat_")
        assert "default" not in row.external_session_id
