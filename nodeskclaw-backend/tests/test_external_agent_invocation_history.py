"""tool 型插件调用历史（GET /{agent_id}/invocations + invoke 落库）测试。

覆盖范围：
- 用户隔离：A 用户看不到 B 用户的调用记录（同 org 也一样按 user_id 过滤）
- 跨 org agent 访问 → 404（IDOR 防护，与其它端点一致）
- invoke 成功 / 上游非 2xx / 上游不可达 三条路径都会落历史
- result_data 超 50KB 截断、params_summary 超长截断（service 层单元测试）
- limit 参数生效

设计约束：
- 走 TestSessionLocal + dependency_overrides[get_current_user] 模式（与
  test_external_agent_functions_api.py 一致）；
- 上游 httpx 用 _FakeAsyncClient 桩掉，不发真实网络请求；
- 限流桶每测重置（invoke 端点先于鉴权做令牌桶检查）。
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.external_agent_invocation import ExternalAgentInvocation
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import external_agent_rate_limit as rl_module
from app.services.external_agent_rate_limit import reset_buckets
from app.services.external_agent_invocation_service import (
    cap_result_data,
    summarize_params,
)
from sqlalchemy import select
from tests.conftest import TestSessionLocal


# ── helpers ─────────────────────────────────────────────────────────────────


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_users(role: str = OrgRole.member, extra_users: int = 0):
    """建一个组织 + 主用户（可选追加同 org 用户），返回 (users, org)。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"inv-org-{suffix}",
            slug=f"inv-org-{suffix}",
            external_agent_allowed_cidrs=["127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        users: list[User] = []
        for i in range(1 + extra_users):
            user = User(
                email=f"inv-{suffix}-{i}@example.com",
                name=f"inv-{suffix}-{i}",
                password_hash="x",
                current_org_id=org.id,
            )
            db.add(user)
            await db.flush()
            db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
            users.append(user)
        await db.commit()
        for u in users:
            await db.refresh(u)
        return users, org


async def _make_tool_agent(*, org_id: str, is_reachable: bool = True) -> str:
    """建一个 active tool 型插件 + default function（表单 invoke 的最小可用形态）。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        invoke_config = {
            "endpoint": "http://127.0.0.1:9999/tool",
            "method": "POST",
            "auth": {"type": "none"},
            "timeout_seconds": 30,
            "pass_mode": "url_ref",
        }
        input_schema = {
            "order": ["line"],
            "fields": {
                "line": {"type": "string", "ui": "select", "label": "产线",
                         "required": True, "options": ["L1", "L2"]},
            },
        }
        agent = ExternalAgent(
            org_id=org_id,
            name=f"inv-tool-agent-{suffix}",
            endpoint="http://127.0.0.1:9999",
            protocol="openai_compatible",
            type="tool",
            invoke_config=invoke_config,
            input_schema=input_schema,
            status="active",
            is_reachable=is_reachable,
        )
        db.add(agent)
        await db.flush()
        db.add(ExternalAgentFunction(
            agent_id=agent.id,
            name="default",
            summary=None,
            invoke_config=dict(invoke_config),
            input_schema=dict(input_schema),
            status="active",
            sort_order=0,
            source="manual",
            version=1,
        ))
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def _insert_invocation(
    *, agent_id: str, org_id: str, user_id: str,
    function_name: str = "default", success: bool = True,
    created_at: datetime | None = None,
) -> str:
    """直接落一条历史记录（不经过 invoke 端点，用于列表查询类测试）。"""
    async with TestSessionLocal() as db:
        row = ExternalAgentInvocation(
            agent_id=agent_id,
            org_id=org_id,
            user_id=user_id,
            function_id=None,
            function_name=function_name,
            params_summary='{"line": "L1"}',
            success=success,
            upstream_status=200 if success else 502,
            latency_ms=12,
            result_data={"success": success, "data": {"ok": 1}},
            error_message=None if success else "Bad Gateway",
            created_at=created_at or datetime.now(timezone.utc),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：把 request 转发给预置 handler（不发真实网络）。"""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self._handler = self.__class__._handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        return await type(self)._handler(method, url, **kwargs)


def _patch_tool_http(handler):
    """桩掉 invoke_tool 里的 httpx.AsyncClient（与 tool_invoke 测试同款手法）。"""
    _FakeAsyncClient._handler = staticmethod(handler)
    return patch(
        "app.services.external_agent_tool_service.httpx.AsyncClient",
        side_effect=lambda *a, **kw: _FakeAsyncClient(*a, **kw),
    )


@pytest.fixture(autouse=True)
def _isolate_rate_limit():
    """每个测试前后清空限流桶，避免 invoke 令牌桶跨测污染。"""
    rl_module.set_enabled(True)
    reset_buckets()
    yield
    reset_buckets()
    rl_module.set_enabled(True)
    _clear_override()


# ── 1. 用户隔离：只能看到自己的记录 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_only_own_records(client: AsyncClient):
    users, org = await _make_org_users(extra_users=1)
    agent_id = await _make_tool_agent(org_id=org.id)
    base = datetime.now(timezone.utc)
    own_ids = [
        await _insert_invocation(
            agent_id=agent_id, org_id=org.id, user_id=str(users[0].id),
            created_at=base - timedelta(minutes=1),
        ),
        await _insert_invocation(
            agent_id=agent_id, org_id=org.id, user_id=str(users[0].id),
            function_name="query", created_at=base,
        ),
    ]
    other_id = await _insert_invocation(
        agent_id=agent_id, org_id=org.id, user_id=str(users[1].id),
        created_at=base,
    )

    _override_user(users[0])
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 只见自己的 2 条（users[1] 的 1 条不可见）
        assert len(data) == 2
        assert {r["id"] for r in data} == set(own_ids)
        assert other_id not in {r["id"] for r in data}
        assert {r["function_name"] for r in data} == {"default", "query"}
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_list_orders_desc_and_respects_limit(client: AsyncClient):
    users, org = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org.id)
    base = datetime.now(timezone.utc)
    ids = []
    for i in range(3):
        ids.append(await _insert_invocation(
            agent_id=agent_id, org_id=org.id, user_id=str(users[0].id),
            created_at=base - timedelta(minutes=3 - i),
        ))

    _override_user(users[0])
    try:
        resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/invocations?limit=2"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        # created_at 倒序：最新（i=2）在前，且最旧（i=0）被 limit 截掉
        assert [r["id"] for r in data] == [ids[2], ids[1]]
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_list_cross_org_agent_returns_404(client: AsyncClient):
    """B 组织用户访问 A 组织 agent 的历史 → 404（不泄露 agent 存在性）。"""
    users_a, org_a = await _make_org_users()
    users_b, _org_b = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org_a.id)
    await _insert_invocation(
        agent_id=agent_id, org_id=org_a.id, user_id=str(users_a[0].id),
    )

    _override_user(users_b[0])
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        assert resp.status_code == 404
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_list_empty_returns_empty_array(client: AsyncClient):
    users, org = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(users[0])
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
    finally:
        _clear_override()


# ── 2. invoke 端点落历史 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_function_success_records_history(client: AsyncClient):
    users, org = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org.id)
    async with TestSessionLocal() as db:
        fn = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id
            )
        )).scalar_one()

    async def handler(method, url, **kwargs):
        return httpx.Response(200, json={"data": [{"code": "A1"}]}, request=httpx.Request("POST", url))

    _override_user(users[0])
    try:
        with _patch_tool_http(handler):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn.id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["success"] is True

        hist = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        data = hist.json()["data"]
        assert len(data) == 1
        row = data[0]
        assert row["success"] is True
        assert row["function_id"] == fn.id
        assert row["function_name"] == "default"
        assert row["params_summary"] == '{"line": "L1"}'
        assert row["upstream_status"] == 200
        assert isinstance(row["latency_ms"], int)
        # result_data 保留完整响应供重放
        assert row["result_data"]["success"] is True
        assert row["result_data"]["data"] == {"data": [{"code": "A1"}]}
        assert row["error_message"] is None
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_function_upstream_failure_records_history(client: AsyncClient):
    users, org = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org.id)

    async def handler(method, url, **kwargs):
        return httpx.Response(500, json={"message": "upstream boom"},
                              request=httpx.Request("POST", url))

    async with TestSessionLocal() as db:
        fn = (await db.execute(
            select(ExternalAgentFunction).where(ExternalAgentFunction.agent_id == agent_id)
        )).scalar_one()

    _override_user(users[0])
    try:
        with _patch_tool_http(handler):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn.id}/invoke",
                json={"params": {"line": "L1"}},
            )
        # 上游 5xx：HTTP 200 + success=false
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is False

        hist = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        data = hist.json()["data"]
        assert len(data) == 1
        row = data[0]
        assert row["success"] is False
        assert row["upstream_status"] == 500
        assert "upstream boom" in (row["error_message"] or "")
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_function_unreachable_records_history(client: AsyncClient):
    """上游连接失败（503 路径）也落一条 success=false 历史。"""
    users, org = await _make_org_users()
    agent_id = await _make_tool_agent(org_id=org.id)

    async def handler(method, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    async with TestSessionLocal() as db:
        fn = (await db.execute(
            select(ExternalAgentFunction).where(ExternalAgentFunction.agent_id == agent_id)
        )).scalar_one()

    _override_user(users[0])
    try:
        with _patch_tool_http(handler):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn.id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 503

        hist = await client.get(f"/api/v1/external-agents/{agent_id}/invocations")
        data = hist.json()["data"]
        assert len(data) == 1
        assert data[0]["success"] is False
        assert data[0]["error_message"] is not None
    finally:
        _clear_override()


# ── 3. service 层截断逻辑（50KB / 摘要长度） ────────────────────────────────


def test_cap_result_data_keeps_small_result():
    result = {"success": True, "data": {"rows": [1, 2, 3]},
              "resolved_items": [1, 2, 3], "display": "table"}
    capped = cap_result_data(result)
    assert capped is not None
    # resolved_items 属冗余，剔除后不落库
    assert "resolved_items" not in capped
    assert capped["data"] == {"rows": [1, 2, 3]}


def test_cap_result_data_truncates_over_50kb():
    big = {"success": True, "data": {"blob": "x" * (60 * 1024)}}
    capped = cap_result_data(big)
    assert capped is not None
    assert capped.get("truncated") is True
    assert capped["size_bytes"] > 50 * 1024
    # 截断后的落库体积必须显著小于原结果
    import json as _json
    assert len(_json.dumps(capped)) < 1024


def test_summarize_params_truncates_long_values():
    params = {"q": "y" * 5000}
    summary = summarize_params(params)
    assert summary is not None
    assert len(summary) <= 2000 + len("...(truncated)")
    assert summary.endswith("...(truncated)")


def test_summarize_params_empty_returns_none():
    assert summarize_params(None) is None
    assert summarize_params({}) is None
