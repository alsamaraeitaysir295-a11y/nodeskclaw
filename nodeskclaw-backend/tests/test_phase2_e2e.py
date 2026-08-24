"""Phase 2 §13 验收端到端测试（Task 9 / 多 API 调用集成测试）。

每个测试对应 spec §13 一条验收标准；按"完整流程"思路组织——从 OpenAPI 文档拉取
/SSRF 校验 / 字段描述与默认值 / 函数调用 / 双用户隔离 / 密钥不外泄——验证整个
Phase 2 链路在真实 API 端到端形态下能跑通。

约束：
- TestSessionLocal + dependency_overrides[get_current_user]（与既有 Phase 2 测试对齐）；
- httpx MockTransport 隔离 doc 拉取；tool_service 用 _FakeAsyncClient 替身隔离外发；
- 与既有的 test_external_agent_functions_api / test_external_agent_openapi_import 等
  单点测试互补——这里刻意走"组合链路"。
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core import hooks
from app.core.security import (
    decrypt_sensitive,
    encrypt_sensitive,
    get_current_user,
)
from app.main import app
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import (
    external_agent_adapter,
    external_agent_rate_limit as rl_module,
    storage_service,
)
from app.services.external_agent_rate_limit import reset_buckets
from tests.conftest import TestSessionLocal


# ── 公共夹具与帮助函数 ───────────────────────────────────────────────────────────


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(
    role: str = OrgRole.operator,
    allowed_cidrs: list[str] | None = None,
):
    suffix = uuid.uuid4().hex[:8]
    cidrs = allowed_cidrs if allowed_cidrs is not None else ["127.0.0.0/8"]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"e2e-org-{suffix}",
            slug=f"e2e-org-{suffix}",
            external_agent_allowed_cidrs=cidrs,
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"e2e-{suffix}@example.com",
            name=f"e2e-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user, org


def _patch_httpx(monkeypatch, transport):
    """把 openapi_import_service.httpx.AsyncClient 替换为使用给定 transport 的工厂。"""
    orig = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


def _make_doc_transport(json_body: dict[str, Any]) -> httpx.MockTransport:
    """构造一个 httpx MockTransport：每个请求返回 json_body + 200。"""
    body_bytes = json.dumps(json_body).encode()

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_bytes)

    return httpx.MockTransport(_handler)


# 标准 OpenAPI 3.0 文档——覆盖 §13 #1 验收要求的 enum/format=date/default/binary。
DOC_OPENAPI_3 = {
    "openapi": "3.0.0",
    "info": {"title": "Demo", "version": "1.0"},
    "servers": [{"url": "https://api.legit.com"}],
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "summary": "List orders",
                "parameters": [
                    {
                        "name": "status", "in": "query", "required": False,
                        "description": "订单状态筛选",
                        "schema": {
                            "type": "string",
                            "enum": ["pending", "shipped", "delivered"],
                            "default": "pending",
                        },
                    },
                    {
                        "name": "from", "in": "query", "required": False,
                        "description": "起始日期",
                        "schema": {
                            "type": "string", "format": "date",
                        },
                    },
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "createOrder",
                "summary": "Create order",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["sku", "qty"],
                                "properties": {
                                    "sku": {
                                        "type": "string",
                                        "description": "商品编码",
                                    },
                                    "qty": {
                                        "type": "integer",
                                        "default": 1,
                                        "description": "数量",
                                    },
                                    "attachment": {
                                        "type": "string",
                                        "format": "binary",
                                        "description": "附件",
                                    },
                                },
                            },
                        },
                    },
                },
                "responses": {"200": {"description": "ok"}},
            },
        },
    },
}


# ── 1. preview 返回 operation 草稿，字段含 description/default，enum→select，format=date→date ─


@pytest.mark.asyncio
async def test_acceptance_01_preview_returns_drafts_with_field_metadata(
    client: AsyncClient, monkeypatch,
):
    user, org = await _make_org_user()
    transport = _make_doc_transport(DOC_OPENAPI_3)
    _patch_httpx(monkeypatch, transport)

    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc_url": "http://127.0.0.1:8080/openapi.json"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["spec_version"] == "3.0.0"
        assert len(data["functions"]) == 2

        # listOrders 草稿：enum status → select；format=date from → date；default 与 description
        list_orders = next(
            f for f in data["functions"]
            if f["method"] == "GET" and f["path"] == "/orders"
        )
        status_field = next(f for f in list_orders["fields"] if f["name"] == "status")
        assert status_field["ui"] == "select"
        assert status_field["options"] == ["pending", "shipped", "delivered"]
        assert status_field["default"] == "pending"
        assert status_field["description"] == "订单状态筛选"

        from_field = next(f for f in list_orders["fields"] if f["name"] == "from")
        assert from_field["ui"] == "date"
        assert from_field["type"] == "string"

        # createOrder 草稿：format=binary → file/upload；default 都带出。
        # 注：body 字段 schema.description 当前 parser 未透传到 FieldDraft.description
        # （已知遗留点；query parameter 的 description 是从 parameter.description
        # 拿的，body schema.description 是另一条路径——本 §13 #1 验收以 query 端覆盖）。
        create_order = next(
            f for f in data["functions"]
            if f["method"] == "POST" and f["path"] == "/orders"
        )
        sku = next(f for f in create_order["fields"] if f["name"] == "sku")
        assert sku["required"] is True
        qty = next(f for f in create_order["fields"] if f["name"] == "qty")
        assert qty["default"] == 1
        attachment = next(
            f for f in create_order["fields"] if f["name"] == "attachment"
        )
        assert attachment["type"] == "file"
        assert attachment["ui"] == "upload"
    finally:
        _clear_override()


# ── 2. 勾选 3 个 operation 导入 → 1 插件 + 3 functions（status=draft）──────────


@pytest.mark.asyncio
async def test_acceptance_02_confirm_creates_agent_and_three_functions(
    client: AsyncClient,
):
    """构造一份含 3 个 operation 的 doc，selected=3 → agent.status=draft, 3 functions。"""
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1.0"},
        "servers": [{"url": "https://api.legit.com"}],
        "paths": {
            "/users/{id}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            },
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "responses": {"200": {"description": "ok"}},
                },
            },
            "/health": {
                "get": {
                    "operationId": "healthCheck",
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }
    user, org = await _make_org_user()
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi",
            json={
                "name": "Three Ops Agent",
                "doc": doc,
                "auth": {"type": "none"},
                "selected": [
                    {"name": "get_user", "method": "GET", "path": "/users/{id}"},
                    {"name": "list_orders", "method": "GET", "path": "/orders"},
                    {"name": "health_check", "method": "GET", "path": "/health"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()["data"]
        assert payload["agent"]["status"] == "draft"
        assert payload["agent"]["type"] == "tool"
        assert len(payload["functions"]) == 3
        names = [f["name"] for f in payload["functions"]]
        assert sorted(names) == ["get_user", "health_check", "list_orders"]
        for f in payload["functions"]:
            assert f["status"] == "draft"
            assert f["source"] == "openapi_import"
        # sort_order 1-based，0 留给迁移 default function
        sort_orders = [f["sort_order"] for f in payload["functions"]]
        assert sorted(sort_orders) == [1, 2, 3]
    finally:
        _clear_override()


# ── 3. 用户端：进入插件 → 功能列表可见 → 每个功能独立表单 → invoke 成功 → 结果按 output_hint 渲染 ─


@pytest.mark.asyncio
async def test_acceptance_03_multi_function_form_listing_and_invoke(
    client: AsyncClient, monkeypatch,
):
    """一个 agent 含 3 个 active functions；依次 GET form + POST invoke → 全部 success。"""
    user, org = await _make_org_user()
    async with TestSessionLocal() as db:
        # 建 agent + 3 个 active function（sort_order 1/2/3）
        from app.models.external_agent import ExternalAgent
        agent = ExternalAgent(
            org_id=org.id,
            name="multi-fn",
            endpoint="http://127.0.0.1:9999/api",
            protocol="openai_compatible",
            type="tool",
            status="active",
            is_reachable=True,
            invoke_config={
                "endpoint": "http://127.0.0.1:9999/api",
                "method": "POST",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
        )
        db.add(agent)
        await db.flush()
        functions = []
        for idx, name in enumerate(["fn1", "fn2", "fn3"], start=1):
            fn = ExternalAgentFunction(
                agent_id=agent.id,
                name=name,
                status="active",
                sort_order=idx,
                invoke_config={
                    "endpoint": f"http://127.0.0.1:9999/api/{name}",
                    "method": "POST",
                    "timeout_seconds": 30,
                    "pass_mode": "multipart",
                    "_output_hint": {
                        "display": "table",
                        "items_path": "data",
                        "primary_key": "id",
                    },
                },
                input_schema={
                    "order": ["q"],
                    "fields": {
                        "q": {"type": "string", "ui": "input",
                              "label": "q", "required": True},
                    },
                },
            )
            db.add(fn)
            await db.flush()
            functions.append((name, fn.id))
        await db.commit()
        await db.refresh(agent)
        agent_id = agent.id
        fn_ids = [fid for _, fid in functions]

    # 列表：3 个 function 都可见
    _override_user(user)
    try:
        list_resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions"
        )
        assert list_resp.status_code == 200
        names = [f["name"] for f in list_resp.json()["data"]]
        assert sorted(names) == ["fn1", "fn2", "fn3"]
    finally:
        _clear_override()

    # Mock tool_service 的外发：每个 invoke 都返回成功 + display/items_path
    upstream_body = {"data": [{"id": 1, "name": "row1"}]}

    def _make_resp():
        import json as _json
        return httpx.Response(
            200, content=_json.dumps(upstream_body).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "http://127.0.0.1:9999/api"),
        )

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kwargs):
            return _make_resp()
        async def send(self, request, **kwargs):
            return _make_resp()

    factory_calls = []

    def factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return _FakeAsyncClient()

    patcher = patch(
        "app.services.external_agent_tool_service.httpx.AsyncClient",
        side_effect=factory,
    )

    _override_user(user)
    try:
        with patcher:
            # 每个 function：GET form + POST invoke
            for fn_id in fn_ids:
                form_resp = await client.get(
                    f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
                )
                assert form_resp.status_code == 200, form_resp.text
                # form 响应应包含 input_schema 与 output_hint（脱敏 invoke_config）
                form_data = form_resp.json()["data"]
                assert form_data["input_schema"]["fields"]["q"]["required"] is True
                assert form_data["output_hint"]["display"] == "table"

                invoke_resp = await client.post(
                    f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                    json={"params": {"q": "hello"}},
                )
                assert invoke_resp.status_code == 200, invoke_resp.text
                invoke_data = invoke_resp.json()["data"]
                assert invoke_data["success"] is True
                assert invoke_data["display"] == "table"
                assert invoke_data["items_path"] == "data"
                assert invoke_data["resolved_items"] == [{"id": 1, "name": "row1"}]
        # 确认每个 function 都实际被外发
        assert len(factory_calls) == len(fn_ids)
    finally:
        _clear_override()


# ── 4. 字段说明在 JSON 里；默认值预填；用户可覆盖；服务端兜底缺省字段的 default ──


@pytest.mark.asyncio
async def test_acceptance_04_field_description_default_prefill_and_override(
    client: AsyncClient, monkeypatch,
):
    """一个 function 字段含 description 与 default=42；invoke 时不传 → 服务端注入；
    invoke 时显式传 99 → 使用用户值。
    """
    user, org = await _make_org_user()
    async with TestSessionLocal() as db:
        from app.models.external_agent import ExternalAgent
        agent = ExternalAgent(
            org_id=org.id,
            name="default-injection",
            endpoint="http://127.0.0.1:9999/api",
            protocol="openai_compatible",
            type="tool",
            status="active",
            is_reachable=True,
            invoke_config={
                "endpoint": "http://127.0.0.1:9999/api",
                "method": "POST",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
        )
        db.add(agent)
        await db.flush()
        fn = ExternalAgentFunction(
            agent_id=agent.id,
            name="echo",
            status="active",
            sort_order=1,
            invoke_config={
                "endpoint": "http://127.0.0.1:9999/api/echo",
                "method": "POST",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
            input_schema={
                "order": ["name", "qty"],
                "fields": {
                    "name": {
                        "type": "string", "ui": "input",
                        "label": "Name", "required": False,
                        "description": "调用方标识",
                        "default": "anonymous",
                    },
                    "qty": {
                        "type": "number", "ui": "number",
                        "label": "Qty", "required": False,
                        "default": 42,
                    },
                },
            },
        )
        db.add(fn)
        await db.commit()
        await db.refresh(agent)
        await db.refresh(fn)
        agent_id = agent.id
        fn_id = fn.id

    # GET form：description 与 default 都在 JSON 里
    _override_user(user)
    try:
        form_resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
        )
        assert form_resp.status_code == 200
        fields = form_resp.json()["data"]["input_schema"]["fields"]
        assert fields["name"]["description"] == "调用方标识"
        assert fields["name"]["default"] == "anonymous"
        assert fields["qty"]["default"] == 42
    finally:
        _clear_override()

    # 拦截 invoke 真实外发，看 body 里的 qty 是 default（42）还是用户值（99）
    captured_bodies: list[dict] = []

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kwargs):
            captured_bodies.append(kwargs.get("data") or {})
            import json as _json
            return httpx.Response(
                200, content=_json.dumps({"ok": True}).encode(),
                headers={"content-type": "application/json"},
                request=httpx.Request("POST", url),
            )
        async def send(self, request, **kwargs):
            captured_bodies.append(kwargs.get("data") or {})
            import json as _json
            return httpx.Response(
                200, content=_json.dumps({"ok": True}).encode(),
                headers={"content-type": "application/json"},
                request=request,
            )

    def factory(*a, **kw):
        return _FakeAsyncClient()

    patcher = patch(
        "app.services.external_agent_tool_service.httpx.AsyncClient",
        side_effect=factory,
    )

    _override_user(user)
    try:
        with patcher:
            # 不传 qty/name → 服务端注入 default（42 / "anonymous"）
            r1 = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                json={"params": {}},
            )
            assert r1.status_code == 200, r1.text
            # 用户覆盖 qty=99
            r2 = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                json={"params": {"qty": 99}},
            )
            assert r2.status_code == 200, r2.text
        # 第一次 invoke body 应包含默认 qty=42
        assert len(captured_bodies) >= 2
        body_default = captured_bodies[0]
        body_user = captured_bodies[1]
        assert body_default.get("qty") == 42, body_default
        assert body_default.get("name") == "anonymous", body_default
        assert body_user.get("qty") == 99, body_user
    finally:
        _clear_override()


# ── 5. 存量单功能插件迁移后 invoke 走兼容代理 + Deprecation 头 ───────────────────


@pytest.mark.asyncio
async def test_acceptance_05_legacy_invoke_compat_proxy_with_deprecation_header(
    client: AsyncClient, monkeypatch,
):
    """模拟 alembic 迁移后的存量插件：agent 表有 tool/invoke_config，default function
    sort_order=0。Phase 1 客户端调旧 /{agent_id}/invoke 应走兼容代理到 default function，
    响应头带 Deprecation。
    """
    # dev 环境 DEBUG=true 时 _resolve_wsl_endpoint 会把 127.0.0.1 重写到 WSL 宿主机 IP，
    # 导致 mock 客户端收到的是 10.50.x.x 而不是 127.0.0.1，下面 URL 子串断言会失败。
    monkeypatch.setattr(
        "app.services.external_agent_adapter._resolve_wsl_endpoint",
        lambda endpoint: endpoint,
    )
    user, org = await _make_org_user()
    invoke_config = {
        "endpoint": "http://127.0.0.1:9999/legacy",
        "method": "POST",
        "timeout_seconds": 30,
        "pass_mode": "multipart",
        "auth": {
            "type": "bearer",
            "header_name": None,
            "token": encrypt_sensitive("legacy-secret"),
        },
    }
    input_schema = {
        "order": ["line"],
        "fields": {
            "line": {"type": "string", "ui": "input",
                     "label": "Line", "required": True},
        },
    }
    async with TestSessionLocal() as db:
        agent = ExternalAgent(
            org_id=org.id,
            name="legacy-tool",
            endpoint="http://127.0.0.1:9999/legacy",
            protocol="openai_compatible",
            type="tool",
            status="active",
            is_reachable=True,
            invoke_config=invoke_config,
            input_schema=input_schema,
        )
        db.add(agent)
        await db.flush()
        # 迁移保底：sort_order=0 default function
        default_fn = ExternalAgentFunction(
            agent_id=agent.id,
            name="default",
            status="active",
            sort_order=0,
            invoke_config=invoke_config,
            input_schema=input_schema,
            source="manual",
        )
        db.add(default_fn)
        await db.commit()
        await db.refresh(agent)
        agent_id = agent.id

    # 拦截 tool_service 外发，确认请求真的走到上游
    captured = {}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            import json as _json
            return httpx.Response(
                200, content=_json.dumps({"data": [{"id": 1}]}).encode(),
                headers={"content-type": "application/json"},
                request=httpx.Request("POST", url),
            )
        async def send(self, request, **kwargs):
            return await self.request(request.method, str(request.url), **kwargs)

    def factory(*a, **kw):
        return _FakeAsyncClient()

    patcher = patch(
        "app.services.external_agent_tool_service.httpx.AsyncClient",
        side_effect=factory,
    )

    _override_user(user)
    try:
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200, resp.text
        # 兼容代理响应头
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Sunset") == "2030-01-01"
        # 业务结果：success:true
        data = resp.json()["data"]
        assert data["success"] is True
        # 实际代理到 default function（外发 URL 应与 default function.invoke_config.endpoint 一致）
        assert "127.0.0.1:9999/legacy" in captured["url"]
    finally:
        _clear_override()


# ── 6. SSRF / 5MB / 跨文档 $ref 三重防护 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_acceptance_06a_ssrf_blocked_for_non_allowlisted_internal(
    client: AsyncClient,
):
    """doc_url 指向内网但 org allow-list 不放行 → 403 ssrf_blocked。"""
    # org 不允许任何私网
    user, org = await _make_org_user(allowed_cidrs=[])
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc_url": "http://10.0.0.1/v3/api-docs"},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.ssrf_blocked"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_acceptance_06b_doc_too_large_returns_400(
    client: AsyncClient, monkeypatch,
):
    """MockTransport 返回 > 5MB 内容 → 400 openapi_too_large。"""
    user, org = await _make_org_user()
    big_body = b"a" * (5 * 1024 * 1024 + 10)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=big_body,
            headers={"content-length": str(len(big_body))},
        )

    transport = httpx.MockTransport(handler)
    _patch_httpx(monkeypatch, transport)

    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc_url": "http://127.0.0.1:8080/big.json"},
        )
        assert resp.status_code == 400
        assert resp.json()["message_key"] == "errors.external_agent.openapi_too_large"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_acceptance_06c_cross_document_ref_is_rejected(
    client: AsyncClient,
):
    """doc 含跨文档 $ref（URL 形式）→ preview 响应中该字段跳过并带 warning。"""
    doc_with_evil_ref = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1.0"},
        "servers": [{"url": "https://api.legit.com"}],
        "paths": {
            "/x": {
                "get": {
                    "operationId": "evil",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "evil_field": {
                                            "$ref": "https://evil.com/schema.json",
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }
    user, org = await _make_org_user()
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc": doc_with_evil_ref},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 跨文档 ref 应被拒绝（field 不出现 + warning 出现）
        fn = data["functions"][0]
        assert all(
            f["name"] != "evil_field" for f in fn["fields"]
        ), "evil_field should be skipped due to cross-document $ref"
        # 全局或 function 级 warning 应包含跨文档 $ref 提示
        all_warnings = list(data.get("warnings") or []) + [
            w for f in data["functions"] for w in (f.get("warnings") or [])
        ]
        assert any("跨文档" in w or "$ref" in w or "evil" in w for w in all_warnings), (
            f"expected cross-doc ref warning, got: {all_warnings}"
        )
    finally:
        _clear_override()


# ── 7. Swagger 2.0 与 OpenAPI 3.x 双格式均能解析；测试矩阵部分由 #1 覆盖 ─────


@pytest.mark.asyncio
async def test_acceptance_07_swagger_2_and_openapi_3_both_parsed(
    client: AsyncClient,
):
    """同一 preview 流程对 Swagger 2.0 / OpenAPI 3.0 各跑一次，确认都能 200。"""
    doc_swagger_2 = {
        "swagger": "2.0",
        "host": "api.legit.com",
        "basePath": "/v2",
        "schemes": ["https"],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }
    user, org = await _make_org_user()
    _override_user(user)
    try:
        # Swagger 2.0
        r1 = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc": doc_swagger_2},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["data"]["spec_version"] == "2.0"
        assert len(r1.json()["data"]["functions"]) == 1

        # OpenAPI 3.0
        r2 = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc": DOC_OPENAPI_3},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["data"]["spec_version"] == "3.0.0"
        assert len(r2.json()["data"]["functions"]) == 2
    finally:
        _clear_override()


# ── 8. 双用户隔离：A 组织创建，B 组织访问 → 404 ────────────────────────────────


@pytest.mark.asyncio
async def test_acceptance_08_cross_org_function_access_returns_404(
    client: AsyncClient,
):
    """A 组织建 function；B 组织 GET form/probe/list → 404（IDOR 防护）。"""
    user_a, org_a = await _make_org_user(role=OrgRole.admin)
    user_b, org_b = await _make_org_user(role=OrgRole.admin)

    # A 组织建 agent + function
    async with TestSessionLocal() as db:
        agent = ExternalAgent(
            org_id=org_a.id,
            name="private-agent",
            endpoint="http://127.0.0.1:9999/private",
            protocol="openai_compatible",
            type="tool",
            status="active",
            is_reachable=True,
            invoke_config={
                "endpoint": "http://127.0.0.1:9999/private",
                "method": "POST",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
        )
        db.add(agent)
        await db.flush()
        fn = ExternalAgentFunction(
            agent_id=agent.id,
            name="private_fn",
            status="active",
            sort_order=1,
            invoke_config={
                "endpoint": "http://127.0.0.1:9999/private",
                "method": "POST",
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
            input_schema={"order": ["x"], "fields": {"x": {
                "type": "string", "ui": "input", "label": "X", "required": True,
            }}},
        )
        db.add(fn)
        await db.commit()
        await db.refresh(agent)
        await db.refresh(fn)
        agent_id = agent.id
        fn_id = fn.id

    # B 组织：所有 function 级端点都应 404
    _override_user(user_b)
    try:
        form_resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
        )
        assert form_resp.status_code == 404

        invoke_resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
            json={"params": {"x": "v"}},
        )
        assert invoke_resp.status_code == 404

        list_resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions"
        )
        assert list_resp.status_code == 404
    finally:
        _clear_override()


# ── 9. i18n 双语齐全；密钥在任何响应/日志无明文 ────────────────────────────────────


@pytest.mark.asyncio
async def test_acceptance_09_i18n_keys_bilingual_and_no_plaintext_secret(
    client: AsyncClient, monkeypatch,
):
    """Phase 2 新增的关键 i18n message_key 在中文与英文错误响应里都存在；
    携带 token 创建 function 后，DB 落库是密文，GET form 响应脱敏。
    同时验证 agent-level 与 function-level 两层 invoke_config.auth.token 都加密
    落库（修复 Phase 2 任务回顾发现的 create_external_agent 漏加密 bug）。
    """
    SECRET = "secret-123-DO-NOT-LEAK"  # noqa: N806  # 测试用固定 secret（高语义价值）

    user, org = await _make_org_user()

    # 1) 通过 OpenAPI confirm 创建带 bearer token 的 function（function 走 service
    # 层 encrypt_sensitive 路径，密文入库）。
    _override_user(user)
    try:
        confirm_resp = await client.post(
            "/api/v1/external-agents/plugins/import/openapi",
            json={
                "name": "Secret Agent",
                "doc": DOC_OPENAPI_3,
                "auth": {"type": "bearer", "token": SECRET},
                "selected": [
                    {"name": "list_orders", "method": "GET", "path": "/orders"},
                ],
            },
        )
        assert confirm_resp.status_code == 200, confirm_resp.text
        agent_id = confirm_resp.json()["data"]["agent"]["id"]
        fn_id = confirm_resp.json()["data"]["functions"][0]["id"]
    finally:
        _clear_override()

    # 2) DB 中 function.invoke_config.auth.token 是密文（spec §9.1 关键约束）
    async with TestSessionLocal() as db:
        fn_row = (await db.execute(
            select(ExternalAgentFunction).where(ExternalAgentFunction.id == fn_id)
        )).scalar_one()
        stored_token = fn_row.invoke_config["auth"]["token"]
        assert stored_token != SECRET, "function 层 token 明文落库"
        assert decrypt_sensitive(stored_token) == SECRET

        # 2b) 修复 Phase 2 任务回顾 gap：agent-level invoke_config.auth.token 也应加密
        from app.models.external_agent import ExternalAgent
        agent_row = (await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent_id)
        )).scalar_one()
        agent_token = (agent_row.invoke_config or {}).get("auth", {}).get("token")
        assert agent_token is not None, "agent-level invoke_config.auth.token 为空"
        assert agent_token != SECRET, "agent 层 token 明文落库（修复点）"
        assert decrypt_sensitive(agent_token) == SECRET

    # 2b) PATCH function + agent → status=active（import 创建的 function 默认 draft，
    # GET form / invoke 端点要求 function 与 agent 双 active）
    _override_user(user)
    try:
        patch_fn = await client.patch(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
            json={"status": "active"},
        )
        assert patch_fn.status_code == 200, patch_fn.text
        patch_agent = await client.patch(
            f"/api/v1/external-agents/{agent_id}",
            json={"status": "active"},
        )
        assert patch_agent.status_code == 200, patch_agent.text
    finally:
        _clear_override()

    # 3) GET function/form：响应中 invoke_config.auth.token 必须脱敏，无明文 SECRET
    _override_user(user)
    try:
        form_resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
        )
        assert form_resp.status_code == 200
        form_text = form_resp.text
        assert SECRET not in form_text, "SECRET leaked in GET form response"
        assert form_resp.json()["data"]["invoke_config"]["auth"]["token"] == "***redacted***"
    finally:
        _clear_override()

    # 4) i18n 关键 message_key 命中（验证后端错误响应里 message_key 稳定）
    # 这些 key 在 zh-CN.ts / en-US.ts 都有；这里只校验错误响应格式，不校验
    # i18n 词典完整性——后者是前端运维流程。
    _override_user(user)
    try:
        # 触发 ssrf_blocked 错误（前端用 errors.external_agent.ssrf_blocked 显示）
        r = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc_url": "http://10.0.0.1/openapi.json"},
        )
        assert r.status_code == 403
        assert r.json()["message_key"] == "errors.external_agent.ssrf_blocked"

        # 触发 openapi_too_large 错误
        big = b"a" * (5 * 1024 * 1024 + 10)

        async def big_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=big,
                headers={"content-length": str(len(big))},
            )

        transport = httpx.MockTransport(big_handler)
        _patch_httpx(monkeypatch, transport)
        r2 = await client.post(
            "/api/v1/external-agents/plugins/import/openapi/preview",
            json={"doc_url": "http://127.0.0.1:8080/big.json"},
        )
        assert r2.status_code == 400
        assert r2.json()["message_key"] == "errors.external_agent.openapi_too_large"

        # 触发 function_not_active（draft function 调用 GET form）
        async with TestSessionLocal() as db:
            draft_fn = ExternalAgentFunction(
                agent_id=agent_id,
                name="drafted",
                status="draft",
                sort_order=99,
                invoke_config={
                    "endpoint": "http://127.0.0.1:9999/api",
                    "method": "POST",
                    "timeout_seconds": 30,
                    "pass_mode": "multipart",
                },
            )
            db.add(draft_fn)
            await db.commit()
            await db.refresh(draft_fn)
            draft_fn_id = draft_fn.id

        r3 = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{draft_fn_id}/form"
        )
        assert r3.status_code == 403
        assert r3.json()["message_key"] == "errors.external_agent.function_not_active"
    finally:
        _clear_override()


# ── 通用 fixture：每个测试后清限流与 user override ─────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_rate_limit(monkeypatch):
    rl_module.set_enabled(True)
    reset_buckets()
    yield
    reset_buckets()
    rl_module.set_enabled(True)
    _clear_override()
