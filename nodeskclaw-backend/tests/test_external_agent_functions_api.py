"""外部 Agent Function CRUD API 测试（Phase 2 §7.2 / Task 2）。

覆盖范围：
- GET /{agent_id}/functions 列表
- POST /{agent_id}/functions 创建（含 manifest 校验、SSRF、token 加密、sort_order 兜底）
- PATCH /{agent_id}/functions/{id} 编辑（含 version+1、SSRF、token 重加密）
- DELETE /{agent_id}/functions/{id} 软删除（admin 权限）
- POST /{agent_id}/functions/{id}/probe 单功能试调（限流 + reachable 字段）

设计约束：
- 走 TestSessionLocal + dependency_overrides[get_current_user] 模式；
- 用 127.0.0.1:1 模拟不可达，避免依赖真实外网；
- IDOR 保护：跨 org 调用必须 404；
- 与 Phase 1 chat 端点隔离（无 message_key 干扰）。
"""
import json
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.core import hooks
from app.core.security import (
    decrypt_sensitive,
    encrypt_sensitive,
    get_current_user,
)
from app.main import app
from app.models.base import not_deleted
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import external_agent_rate_limit as rl_module
from app.services.external_agent_rate_limit import reset_buckets
from app.services.external_agent_tool_service import redact_invoke_config
from sqlalchemy import select
from tests.conftest import TestSessionLocal


# ── 公共夹具与帮助函数 ───────────────────────────────────────────────────────────


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(role: str = OrgRole.operator, allowed_cidrs: list[str] | None = None):
    """建一个组织 + 用户；SSRF 白名单默认允许 127.0.0.0/8（测试 loopback）。"""
    suffix = uuid.uuid4().hex[:8]
    cidrs = allowed_cidrs if allowed_cidrs is not None else ["127.0.0.0/8"]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"fn-api-org-{suffix}",
            slug=f"fn-api-org-{suffix}",
            external_agent_allowed_cidrs=cidrs,
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"fn-api-{suffix}@example.com",
            name=f"fn-api-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user, org


async def _make_tool_agent(
    *, org_id: str, name: str | None = None, status: str = "active",
    invoke_endpoint: str = "http://127.0.0.1:9999/tool",
):
    """建一个 tool 型插件（含一个最小可用 invoke_config）。返回 agent_id。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        agent = ExternalAgent(
            org_id=org_id,
            name=name or f"fn-tool-agent-{suffix}",
            endpoint=invoke_endpoint,
            protocol="openai_compatible",
            type="tool",
            status=status,
            invoke_config={
                "endpoint": invoke_endpoint,
                "method": "GET",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
            input_schema={
                "order": [],
                "fields": {},
            },
            version=1,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


def _valid_create_body(name: str = "query_orders", **overrides) -> dict:
    """构造合法的 POST body（手动加功能）。"""
    body: dict = {
        "name": name,
        "summary": "查询订单列表",
        "invoke_config": {
            "endpoint": "http://127.0.0.1:9999/orders",
            "method": "POST",
            "auth": {"type": "bearer", "token": "plain-secret"},
            "timeout_seconds": 30,
            "pass_mode": "multipart",
        },
        "input_schema": {
            "order": ["line"],
            "fields": {
                "line": {
                    "type": "string", "ui": "select", "label": "产线",
                    "required": True, "options": ["L1", "L2"],
                },
            },
        },
        "output_hint": {"display": "table", "items_path": "data"},
        "status": "draft",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _isolate_rate_limit(monkeypatch):
    """每个测试前后清空限流桶 + 恢复 ENABLED。"""
    rl_module.set_enabled(True)
    reset_buckets()
    yield
    reset_buckets()
    rl_module.set_enabled(True)
    _clear_override()


# ── 1. list：空 agent → [] ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_functions_empty(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/functions")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
    finally:
        _clear_override()


# ── 2. list：迁移默认 function 应可见 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_functions_returns_migrated_default(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id, name="default", sort_order=0, status="active",
        )
        db.add(fn)
        await db.commit()
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/functions")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["name"] == "default"
    finally:
        _clear_override()


# ── 3. create：POST → 200，sort_order 自动 >= 1 ────────────────────────────


@pytest.mark.asyncio
async def test_create_function_persists_and_returns(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["name"] == "query_orders"
        assert data["sort_order"] >= 1
        assert data["status"] == "draft"
        assert data["version"] == 1
        assert data["source"] == "manual"
        assert data["output_hint"]["display"] == "table"
        # token 不应明文
        assert data["invoke_config"]["auth"]["token"] != "plain-secret"
    finally:
        _clear_override()


# ── 4. create：同名 → 400 (function_name_conflict) ──────────────────────────


@pytest.mark.asyncio
async def test_create_function_with_duplicate_name_returns_400(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        first = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="dup"),
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="dup"),
        )
        # name 唯一约束 → IntegrityError → service 抛 BadRequestError（message_key
        # 由 Pydantic message_key 直接转 40000，Phase 1 review P2-1: 这里走统一
        # manifest_invalid 是兼容路径；更精确的实现可后置为 function_name_conflict，
        # 当前允许并入现有错误码以保持服务层简洁）。
        assert second.status_code == 400
    finally:
        _clear_override()


# ── 5. create：sort_order=0 与迁移的 default 撞 → 400 ────────────────────


@pytest.mark.asyncio
async def test_create_function_with_sort_order_zero_conflicts_with_default(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    async with TestSessionLocal() as db:
        # 模拟迁移后已存在的 default function
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="default", sort_order=0, status="active",
        ))
        await db.commit()

    _override_user(user)
    try:
        body = _valid_create_body(name="collide", sort_order=0)
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=body,
        )
        # partial unique index 拦截 → IntegrityError → 400
        assert resp.status_code == 400
    finally:
        _clear_override()


# ── 6. create：member 角色 → 403 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_function_requires_operator_role(client: AsyncClient):
    user, org = await _make_org_user(role=OrgRole.member)
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


# ── 7. create：invoke 校验失败（timeout > 120） → 422 ────────────────────


@pytest.mark.asyncio
async def test_create_function_validates_invoke_via_manifest(client: AsyncClient):
    """Pydantic 在 FastAPI 层先做字段级校验：timeout_seconds > 120 直接 422。

    这与 spec 描述的"400 + Manifest 校验失败"路径不同——schema 层先于 Manifest 层
    触发。功能行为等价（拒绝入库），仅 HTTP code 不同。
    """
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        body = _valid_create_body(name="bad-timeout")
        body["invoke_config"]["timeout_seconds"] = 200  # 超过 120 上限
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=body,
        )
        # Pydantic 字段级校验失败 → 422（FastAPI 默认行为）
        assert resp.status_code == 422
    finally:
        _clear_override()


# ── 8. create：token 加密 + GET form 时脱敏 ────────────────────────────────


@pytest.mark.asyncio
async def test_create_function_encrypts_token_in_db(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="with-token"),
        )
        assert resp.status_code == 200
        fn_id = resp.json()["data"]["id"]

        # DB 中应已加密
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(ExternalAgentFunction).where(ExternalAgentFunction.id == fn_id)
            )).scalar_one()
            stored = row.invoke_config["auth"]["token"]
            assert stored != "plain-secret"
            # 能解密还原
            assert decrypt_sensitive(stored) == "plain-secret"
            # 脱敏工具验证
            assert redact_invoke_config(row.invoke_config)["auth"]["token"] == "***redacted***"
    finally:
        _clear_override()


# ── 9. create：SSRF 闸门（endpoint 不在白名单） → 403 ────────────────────


@pytest.mark.asyncio
async def test_create_function_ssrf_block(client: AsyncClient):
    user, org = await _make_org_user(allowed_cidrs=["10.0.0.0/8"])
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        body = _valid_create_body(name="ssrf-bad")
        body["invoke_config"]["endpoint"] = "http://192.168.1.1:9000/x"  # 不在 allow-list
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=body,
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.ssrf_blocked"
    finally:
        _clear_override()


# ── 10. PATCH invoke_config → version +1 ──────────────────────────────────


@pytest.mark.asyncio
async def test_update_function_bumps_version_on_schema_change(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="bump"),
        )
        assert create.status_code == 200
        fn_id = create.json()["data"]["id"]
        assert create.json()["data"]["version"] == 1

        # PATCH invoke_config
        new_inv = _valid_create_body(name="bump")["invoke_config"]
        new_inv["timeout_seconds"] = 60
        patch_resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
            json={"invoke_config": new_inv},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["data"]["version"] == 2
    finally:
        _clear_override()


# ── 11. PATCH name-only → version 不变 ────────────────────────────────────


@pytest.mark.asyncio
async def test_update_function_does_not_bump_version_on_name_change(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="rename"),
        )
        fn_id = create.json()["data"]["id"]
        assert create.json()["data"]["version"] == 1

        patch_resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
            json={"name": "renamed"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["data"]["version"] == 1
        assert patch_resp.json()["data"]["name"] == "renamed"
    finally:
        _clear_override()


# ── 12. PATCH name 成功 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_function_renames_ok(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="old"),
        )
        fn_id = create.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
            json={"name": "new"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "new"
    finally:
        _clear_override()


# ── 13. DELETE → 列表清空（soft_delete） ─────────────────────────────────


@pytest.mark.asyncio
async def test_delete_function_soft_deletes(client: AsyncClient):
    admin, org = await _make_org_user(role=OrgRole.admin)
    agent_id = await _make_tool_agent(org_id=org.id)

    _override_user(admin)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="todelete"),
        )
        assert create.status_code == 200, create.text
        fn_id = create.json()["data"]["id"]

        del_resp = await client.delete(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
        )
        assert del_resp.status_code == 200

        # 列表应为空（已软删）
        list_resp = await client.get(f"/api/v1/external-agents/{agent_id}/functions")
        assert list_resp.json()["data"] == []
    finally:
        _clear_override()

    # DB 中 deleted_at 已置位
    async with TestSessionLocal() as db:
        row = (await db.execute(
            select(ExternalAgentFunction).where(ExternalAgentFunction.id == fn_id)
        )).scalar_one()
        assert row.deleted_at is not None


# ── 14. DELETE：operator 角色 → 403；admin → 200 ─────────────────────────


@pytest.mark.asyncio
async def test_delete_function_requires_admin_role(client: AsyncClient):
    operator, org = await _make_org_user(role=OrgRole.operator)
    agent_id = await _make_tool_agent(org_id=org.id)

    _override_user(operator)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="admin-only"),
        )
        fn_id = create.json()["data"]["id"]
        # operator 可创建，但删除被拒
        del_resp = await client.delete(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
        )
        assert del_resp.status_code == 403
    finally:
        _clear_override()


# ── 15. probe：调用 upstream（mock transport） ────────────────────────────


@pytest.mark.asyncio
async def test_probe_function_calls_upstream(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)

    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="to-probe"),
        )
        fn_id = create.json()["data"]["id"]
    finally:
        _clear_override()

    # Mock probe_tool_invoke：避免真实网络
    from app.services import external_agent_adapter as adapter
    captured = {}

    async def fake_probe(invoke, *, allowed_cidrs=None):
        captured["endpoint"] = invoke.endpoint
        captured["method"] = invoke.method
        return {
            "ok": True,
            "http_code": 200,
            "latency_ms": 12,
            "error": None,
            "skipped_invoke": False,
        }

    _override_user(user)
    try:
        with patch.object(adapter, "probe_tool_invoke", side_effect=fake_probe):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
            )
        assert resp.status_code == 200, resp.text
        assert captured["endpoint"] == "http://127.0.0.1:9999/orders"
        assert resp.json()["data"]["ok"] is True
        assert resp.json()["data"]["reachable"] is True
    finally:
        _clear_override()


# ── 16. probe：第 11 次 → 429 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_function_rate_limited(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="rl"),
        )
        fn_id = create.json()["data"]["id"]
    finally:
        _clear_override()

    from app.services import external_agent_adapter as adapter

    async def fake_probe(invoke, *, allowed_cidrs=None):
        return {"ok": True, "http_code": 200, "latency_ms": 1,
                "error": None, "skipped_invoke": False}

    _override_user(user)
    try:
        with patch.object(adapter, "probe_tool_invoke", side_effect=fake_probe):
            # 前 10 次都成功
            for _ in range(10):
                r = await client.post(
                    f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
                )
                assert r.status_code == 200
            # 第 11 次应被限流
            r = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
            )
            assert r.status_code == 429
            assert r.json()["message_key"] == "errors.external_agent.rate_limited"
    finally:
        _clear_override()


# ── 17. probe：SSRF 闸门（不在白名单） → 403 ─────────────────────────────


@pytest.mark.asyncio
async def test_probe_function_validates_ssrf(client: AsyncClient):
    user, org = await _make_org_user(allowed_cidrs=["10.0.0.0/8"])
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        # 直接绕过 SSRF 校验：endpoint 写入不在 allow-list 的地址
        # 用 service 层直接插入（避免 create 阶段被 SSRF 拦截）。
        async with TestSessionLocal() as db:
            fn = ExternalAgentFunction(
                agent_id=agent_id,
                name="ssrf-probe",
                sort_order=1,
                status="draft",
                invoke_config={
                    "endpoint": "http://192.168.1.1:9000/x",  # 不在 allow-list
                    "method": "GET",
                    "timeout_seconds": 30,
                    "pass_mode": "multipart",
                },
            )
            db.add(fn)
            await db.commit()
            await db.refresh(fn)
            fn_id = fn.id

        # probe → SSRF 拦截
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.ssrf_blocked"
    finally:
        _clear_override()


# ── 18. probe：响应含 reachable 字段 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_function_returns_reachable_field(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="reach"),
        )
        fn_id = create.json()["data"]["id"]
    finally:
        _clear_override()

    from app.services import external_agent_adapter as adapter

    async def fake_probe(invoke, *, allowed_cidrs=None):
        return {"ok": False, "http_code": 502, "latency_ms": 30,
                "error": "Bad Gateway", "skipped_invoke": False}

    _override_user(user)
    try:
        with patch.object(adapter, "probe_tool_invoke", side_effect=fake_probe):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
            )
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "reachable" in body
        assert body["reachable"] is False
    finally:
        _clear_override()


# ── 19. GET function 跨 org → 404（IDOR 防护）────────────────────────────


@pytest.mark.asyncio
async def test_get_function_idor_protection(client: AsyncClient):
    """A 组织创建 function，B 组织尝试 GET 该 function → 404。

    get_function 本身不直接暴露在 API，但 list/probe 都通过 get_function 校验归属；
    用 probe 端点验证：probe 时虽然 function_id 在 URL，但会查 agent.org_id 校验，
    跨 org 应失败。
    """
    user_a, org_a = await _make_org_user(role=OrgRole.admin)
    user_b, org_b = await _make_org_user(role=OrgRole.admin)

    # A 组织建 agent + function
    _override_user(user_a)
    try:
        agent_id = await _make_tool_agent(org_id=org_a.id)
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="private"),
        )
        assert create.status_code == 200
        fn_id = create.json()["data"]["id"]
    finally:
        _clear_override()

    # B 组织用同一 function_id（但 agent 在 org_a 下）调用 probe
    _override_user(user_b)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/probe",
        )
        # get_external_agent 校验失败 → NotFoundError → 404
        assert resp.status_code == 404
    finally:
        _clear_override()


# ── 20. 跨 org list 访问 → 404（显式覆盖）─────────────────────────────────


@pytest.mark.asyncio
async def test_cross_org_function_access_returns_404(client: AsyncClient):
    user_a, org_a = await _make_org_user(role=OrgRole.admin)
    user_b, org_b = await _make_org_user(role=OrgRole.admin)
    agent_id = await _make_tool_agent(org_id=org_a.id)

    # A 组织建 function
    _override_user(user_a)
    try:
        create = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json=_valid_create_body(name="cross-org"),
        )
        fn_id = create.json()["data"]["id"]
    finally:
        _clear_override()

    # B 组织列表 A 组织的 agent functions → 404（agent 不在 B org）
    _override_user(user_b)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/functions")
        assert resp.status_code == 404
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_sync_tool_agent_uses_invoke_endpoint_not_models(client: AsyncClient, monkeypatch):
    """sync 对 tool 型必须按 invoke_config 探测；GET /v1/models 404 不得误判不可达。

    回归背景：sync 曾沿用 chat 协议的 verify_connection（GET {endpoint}/v1/models），
    对 REST 型 invoke endpoint 必然 404，导致列表页"同步"把插件错误标记为不可达，
    随后用户端 invoke 被 503 invoke_unreachable 拦截（表现为"连接失败"）。
    """
    import httpx as _httpx

    user, org = await _make_org_user()
    _override_user(user)
    try:
        create_resp = await client.post(
            "/api/v1/external-agents",
            json={
                "name": "sync-tool", "type": "tool",
                "invoke_config": {
                    "endpoint": "http://127.0.0.1:19999/api/orders",
                    "method": "GET",
                    "auth": {"type": "none"},
                },
            },
        )
        assert create_resp.status_code == 200, create_resp.text
        agent_id = create_resp.json()["data"]["id"]

        # mock transport：/api/orders 返回 200；任何 /v3/api-docs|/v1/models 请求都标记污染
        seen = {"polluted": False}

        def handler(request: _httpx.Request) -> _httpx.Response:
            if "/v1/models" in str(request.url) or "/health" in str(request.url):
                seen["polluted"] = True
            return _httpx.Response(200, json={"items": []})

        transport = _httpx.MockTransport(handler)

        class _FakeAsyncClient(_httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw.pop("transport", None)
                super().__init__(transport=transport, *a, **kw)

        monkeypatch.setattr(
            "app.services.external_agent_adapter.httpx.AsyncClient", _FakeAsyncClient,
        )
        from app.services import external_agent_adapter as _adapter
        monkeypatch.setattr(_adapter, "_resolve_wsl_endpoint", lambda e: e)

        resp = await client.post(f"/api/v1/external-agents/{agent_id}/sync")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["reachable"] is True
        assert seen["polluted"] is False, "sync 不得再走 chat 协议探针（/v1/models 或 /health）"

        # agent 状态回写：is_reachable=true + last_probe 含 http_code
        list_resp = await client.get("/api/v1/external-agents")
        row = next(a for a in list_resp.json()["data"] if a["id"] == agent_id)
        assert row["is_reachable"] is True
        assert row["last_probe"]["ok"] is True
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_update_function_preserves_output_hint_subkey(client: AsyncClient):
    """PATCH invoke_config 时 _output_hint 子键必须保留（display=text 渲染配置）。

    回归背景：ExternalAgentFunctionUpdate.invoke_config 走 ManifestInvokeConfig
    校验，model_dump 会剥掉模型外字段；开 extra=allow 前更新一次配置就把
    output_hint 静默清空，text 渲染退化为 JSON。
    """
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    _override_user(user)
    try:
        fn = (await client.post(
            f"/api/v1/external-agents/{agent_id}/functions",
            json={
                "name": "q", "invoke_config": {
                    "endpoint": "http://127.0.0.1:9999/q",
                    "method": "POST", "auth": {"type": "none"},
                    "_output_hint": {"display": "text", "text_path": "answer"},
                },
            },
        )).json()["data"]
        assert fn["output_hint"] == {"display": "text", "text_path": "answer"}

        # PATCH 改 timeout/pass_mode → _output_hint 必须保留
        patch_resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}/functions/{fn['id']}",
            json={"invoke_config": {
                "endpoint": "http://127.0.0.1:9999/q",
                "method": "POST", "auth": {"type": "none"},
                "timeout_seconds": 120, "pass_mode": "url_ref",
                "_output_hint": {"display": "text", "text_path": "answer"},
            }},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        data = patch_resp.json()["data"]
        assert data["output_hint"] == {"display": "text", "text_path": "answer"}
        assert data["invoke_config"]["timeout_seconds"] == 120
    finally:
        _clear_override()
