"""外部 Agent function 级路由测试（Phase 2 §7.3 / §7.4）。

覆盖：
- function 级 /form、/invoke、/files 三个端点（含 active/inactive、跨 agent、限流、SSRF）
- 旧 /{agent_id}/{form,invoke,files} 兼容代理（内部代理到 sort_order=0 function，
  响应头带 Deprecation）
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient

from app.core import hooks
from app.core.security import encrypt_sensitive, get_current_user
from app.main import app
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import storage_service
from app.services import external_agent_rate_limit as rl_module
from app.services.external_agent_rate_limit import reset_buckets
from tests.conftest import TestSessionLocal


# ── 公共夹具与帮助函数 ───────────────────────────────────────────────────────────


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(
    *, role: str = OrgRole.operator, allowed_cidrs: list[str] | None = None,
):
    suffix = uuid.uuid4().hex[:8]
    cidrs = allowed_cidrs if allowed_cidrs is not None else ["127.0.0.0/8"]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"fn-rt-org-{suffix}",
            slug=f"fn-rt-org-{suffix}",
            external_agent_allowed_cidrs=cidrs,
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"fn-rt-{suffix}@example.com",
            name=f"fn-rt-{suffix}",
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
    *,
    org_id: str,
    status: str = "active",
    is_reachable: bool = True,
    input_schema: dict | None = None,
    invoke_config: dict | None = None,
    agent_endpoint: str = "http://127.0.0.1:9999/tool",
):
    """建一个 tool 型插件；agent 列字段也会作为 function 兜底默认。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        agent = ExternalAgent(
            org_id=org_id,
            name=f"fn-rt-agent-{suffix}",
            endpoint=agent_endpoint,
            protocol="openai_compatible",
            type="tool",
            status=status,
            is_reachable=is_reachable,
            invoke_config=invoke_config,
            input_schema=input_schema,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


def _default_invoke_config(
    endpoint: str = "http://127.0.0.1:9999/api",
    method: str = "POST",
    auth_token: str | None = None,
    pass_mode: str = "multipart",
) -> dict:
    ic: dict = {
        "endpoint": endpoint,
        "method": method,
        "timeout_seconds": 30,
        "pass_mode": pass_mode,
    }
    if auth_token is not None:
        ic["auth"] = {
            "type": "bearer", "header_name": None,
            "token": encrypt_sensitive(auth_token),
        }
    if method == "GET":
        ic["_output_hint"] = {"display": "table", "items_path": "data"}
    return ic


def _default_input_schema(
    *, with_file: bool = True, file_max_mb: int | None = None,
) -> dict:
    schema: dict = {
        "order": ["line", "note"],
        "fields": {
            "line": {"type": "string", "ui": "select", "label": "产线",
                     "required": True, "options": ["L1", "L2"]},
            "note": {"type": "string", "ui": "input", "label": "备注",
                     "required": False, "description": "可选说明"},
        },
    }
    if with_file:
        schema["order"].append("file")
        file_def: dict = {"type": "file", "ui": "upload", "label": "明细",
                           "accept": [".xlsx", ".csv"], "required": False}
        if file_max_mb is not None:
            file_def["max_mb"] = file_max_mb
        schema["fields"]["file"] = file_def
    return schema


async def _make_function(
    *, agent_id: str, name: str = "default", sort_order: int = 0,
    status: str = "active", input_schema: dict | None = None,
    invoke_config: dict | None = None, func_status: str | None = None,
):
    """直接 DB 插入一条 ExternalAgentFunction。func_status 兼容错别字（status 优先）。"""
    _status = func_status if func_status is not None else status
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id,
            name=name,
            summary=f"summary of {name}",
            invoke_config=invoke_config,
            input_schema=input_schema,
            status=_status,
            sort_order=sort_order,
            source="manual",
            version=1,
        )
        db.add(fn)
        await db.commit()
        await db.refresh(fn)
        return fn.id


def _make_response(status_code: int, json_body=None) -> httpx.Response:
    if json_body is not None:
        import json as _json
        content = _json.dumps(json_body).encode("utf-8")
    else:
        content = b""
    request = httpx.Request("POST", "http://127.0.0.1:9999")
    return httpx.Response(
        status_code=status_code, content=content,
        headers={"content-type": "application/json"}, request=request,
    )


class _FakeAsyncClient:
    """同 test_external_agent_tool_invoke 的 httpx 替身。"""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        return await self._dispatch(method, url, **kwargs)

    async def send(self, request, **kwargs):
        return await self._dispatch(
            request.method, str(request.url), request=request, **kwargs,
        )

    async def _dispatch(self, method, url, **kwargs):
        handler = getattr(self, "_handler", None)
        if handler is None:
            raise RuntimeError("AsyncClient mock has no handler")
        return await handler(method, url, **kwargs)

    def _set_handler(self, handler):
        self._handler = handler


def _patch_tool_http(handler):
    holder: dict = {}

    def factory(*args, **kwargs):
        c = _FakeAsyncClient(*args, **kwargs)
        c._handler = handler
        holder["client"] = c
        return c

    p = patch(
        "app.services.external_agent_tool_service.httpx.AsyncClient",
        side_effect=factory,
    )
    return p, holder


@pytest.fixture(autouse=True)
def _isolate_rate_limit(monkeypatch):
    rl_module.set_enabled(True)
    reset_buckets()


@pytest.fixture(autouse=True)
def _disable_wsl_endpoint_rewrite(monkeypatch):
    """dev 环境 DEBUG=true 时 `_resolve_wsl_endpoint` 会把 127.0.0.1 改写到
    WSL 宿主机 IP，导致 mock httpx 客户端无法命中。测试场景下全部统一短路。
    """
    from app.services import external_agent_adapter
    monkeypatch.setattr(
        external_agent_adapter,
        "_resolve_wsl_endpoint",
        lambda endpoint: endpoint,
    )


@pytest.fixture(autouse=True)
def _isolate_filesystem_root(monkeypatch, tmp_path):
    """storage_service 默认把文件写到 `~/.nodeskclaw/shared-files/...`，但容器里
    HOME 是 /root，目录可能不可写。改写到 tmp_path 隔离。
    """
    monkeypatch.setattr(storage_service, "_get_local_dir", lambda: tmp_path / "shared-files")
    yield
    reset_buckets()
    rl_module.set_enabled(True)
    _clear_override()


# ── 1. GET /{agent_id}/functions/{function_id}/form：active 检查 ──────────────


@pytest.mark.asyncio
async def test_get_function_form_returns_active_only(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id)
    fn_id = await _make_function(
        agent_id=agent_id, name="drafted", sort_order=1, status="draft",
        input_schema=_default_input_schema(with_file=False),
    )

    _override_user(user)
    try:
        resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.function_not_active"
    finally:
        _clear_override()


# ── 2. GET form：active → 200，返回 input_schema + output_hint + redacted token


@pytest.mark.asyncio
async def test_get_function_form_returns_data(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, invoke_config=_default_invoke_config(),
    )
    fn_input_schema = _default_input_schema(with_file=False)
    fn_invoke_config = _default_invoke_config(auth_token="real-secret")
    fn_id = await _make_function(
        agent_id=agent_id, name="fn1", sort_order=1,
        input_schema=fn_input_schema, invoke_config=fn_invoke_config,
    )

    _override_user(user)
    try:
        resp = await client.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["function_id"] == fn_id
        assert data["agent_id"] == agent_id
        assert data["name"] == "fn1"
        assert data["input_schema"]["fields"]["line"]["required"] is True
        assert data["output_hint"] is None  # GET 不带 _output_hint
        # token 必须脱敏
        assert data["invoke_config"]["auth"]["token"] == "***redacted***"
        assert "real-secret" not in str(data)
    finally:
        _clear_override()


# ── 3. GET form：URL 错配（fn 属于 agent A，URL 是 agent B）→ 404 ──────────


@pytest.mark.asyncio
async def test_get_function_form_wrong_agent_id_returns_404(client: AsyncClient):
    user, org = await _make_org_user()
    agent_a = await _make_tool_agent(org_id=org.id)
    agent_b = await _make_tool_agent(org_id=org.id)
    fn_id = await _make_function(
        agent_id=agent_a, name="under-a", sort_order=1,
        input_schema=_default_input_schema(with_file=False),
    )

    _override_user(user)
    try:
        # 用 agent_b 的 URL 取 agent_a 下的 function → 404
        resp = await client.get(
            f"/api/v1/external-agents/{agent_b}/functions/{fn_id}/form"
        )
        assert resp.status_code == 404
    finally:
        _clear_override()


# ── 4. POST invoke：调用 invoke_tool，参数符合 function schema ──────────────


@pytest.mark.asyncio
async def test_invoke_function_calls_invoke_tool(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, is_reachable=True,
        invoke_config=_default_invoke_config(
            endpoint="http://127.0.0.1:9999/api", method="GET",
        ),
    )
    fn_id = await _make_function(
        agent_id=agent_id, name="inv", sort_order=1,
        invoke_config=_default_invoke_config(
            endpoint="http://127.0.0.1:9999/api", method="GET",
        ),
        input_schema=_default_input_schema(with_file=False),
    )
    captured: dict = {}

    async def handler(method, url, **kwargs):
        captured["method"] = method
        captured["kwargs"] = kwargs
        return _make_response(200, {"data": [{"id": 1}], "ok": True})

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                json={"params": {"line": "L1", "note": "hi"}},
            )
        assert resp.status_code == 200, resp.text
        assert captured["method"] == "GET"
        # GET 模式：line 进 query params
        params = captured["kwargs"]["params"]
        assert params.get("line") == "L1"
    finally:
        _clear_override()


# ── 5. POST invoke：rate-limited 11th call → 429 ────────────────────────────


@pytest.mark.asyncio
async def test_invoke_function_rate_limited(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, is_reachable=True,
        invoke_config=_default_invoke_config(method="POST"),
    )
    fn_id = await _make_function(
        agent_id=agent_id, name="rl", sort_order=1,
        invoke_config=_default_invoke_config(method="POST"),
        input_schema=_default_input_schema(with_file=False),
    )

    async def handler(method, url, **kwargs):
        return _make_response(200, {"ok": True})

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            for _ in range(10):
                r = await client.post(
                    f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                    json={"params": {"line": "L1"}},
                )
                assert r.status_code == 200
            r = await client.post(
                f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert r.status_code == 429
        assert r.json()["message_key"] == "errors.external_agent.rate_limited"
    finally:
        _clear_override()


# ── 6. POST invoke：function.status=draft → 403 function_not_active ────────


@pytest.mark.asyncio
async def test_invoke_function_inactive_returns_403(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id, is_reachable=True)
    fn_id = await _make_function(
        agent_id=agent_id, name="dis", sort_order=1, status="disabled",
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
            json={"params": {"line": "L1"}},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.function_not_active"
    finally:
        _clear_override()


# ── 7. POST invoke：SSRF 闸门（function.endpoint 内网 + 不在白名单）→ 403 ──


@pytest.mark.asyncio
async def test_invoke_function_ssrf_block(client: AsyncClient):
    user, org = await _make_org_user(allowed_cidrs=["10.0.0.0/8"])
    agent_id = await _make_tool_agent(org_id=org.id, is_reachable=True)
    # 直接 DB 插入绕过 SSRF 校验
    fn_id = await _make_function(
        agent_id=agent_id, name="ssrf", sort_order=1,
        invoke_config={
            "endpoint": "http://192.168.1.1:9000/x",
            "method": "POST",
            "timeout_seconds": 30,
            "pass_mode": "multipart",
        },
        input_schema=_default_input_schema(with_file=False),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
            json={"params": {"line": "L1"}},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.ssrf_blocked"
    finally:
        _clear_override()


# ── 8. POST invoke：agent.is_reachable=False → 503 ─────────────────────────


@pytest.mark.asyncio
async def test_invoke_function_unreachable_returns_503(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id, is_reachable=False)
    fn_id = await _make_function(
        agent_id=agent_id, name="ur", sort_order=1,
        invoke_config=_default_invoke_config(),
        input_schema=_default_input_schema(with_file=False),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
            json={"params": {"line": "L1"}},
        )
        assert resp.status_code == 503
        assert resp.json()["message_key"] == "errors.external_agent.invoke_unreachable"
    finally:
        _clear_override()


# ── 9. POST invoke：缺必填字段 → 422 with field_errors ──────────────────────


@pytest.mark.asyncio
async def test_invoke_function_field_error_returns_422(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id, is_reachable=True)
    fn_id = await _make_function(
        agent_id=agent_id, name="v", sort_order=1,
        invoke_config=_default_invoke_config(),
        input_schema=_default_input_schema(with_file=False),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/invoke",
            json={"params": {"note": "no line"}},  # 缺必填 line
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.invoke_validation_error"
        field_errors = body.get("field_errors") or []
        assert any(err.get("field") == "line" for err in field_errors)
    finally:
        _clear_override()


# ── 10. POST /{id}/functions/{fid}/files：function max_mb=5 + 10MB → 400 ──


@pytest.mark.asyncio
async def test_upload_function_file_uses_function_field_max_mb(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id, status="active")
    fn_id = await _make_function(
        agent_id=agent_id, name="big", sort_order=1, status="active",
        input_schema=_default_input_schema(with_file=True, file_max_mb=5),
        invoke_config=_default_invoke_config(),
    )

    _override_user(user)
    try:
        big = b"x" * (10 * 1024 * 1024)  # 10MB
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/files",
            files={"file": ("data.xlsx", big, "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.file_too_large"
        assert body.get("message_params", {}).get("max_mb") == "5"
    finally:
        _clear_override()


# ── 11. POST /{id}/functions/{fid}/files：function 无 file 字段 → 400 ──────


@pytest.mark.asyncio
async def test_upload_function_file_no_file_field_returns_400(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(org_id=org.id, status="active")
    fn_id = await _make_function(
        agent_id=agent_id, name="plain", sort_order=1,
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/files",
            files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
        assert resp.json()["message_key"] == "errors.external_agent.file_upload_not_supported"
    finally:
        _clear_override()


# ── 12. 旧 /form 代理到 default function（spec §7.4）──────────────────────


@pytest.mark.asyncio
async def test_legacy_form_delegates_to_default_function(client: AsyncClient):
    user, org = await _make_org_user()
    # agent input_schema 与 function input_schema 不同：响应应来自 function
    agent_input_schema = _default_input_schema(with_file=False)
    agent_input_schema["fields"]["line"]["label"] = "AGENT_LABEL"
    fn_input_schema = _default_input_schema(with_file=False)
    fn_input_schema["fields"]["line"]["label"] = "FN_LABEL"
    agent_id = await _make_tool_agent(
        org_id=org.id, input_schema=agent_input_schema,
        invoke_config={"endpoint": "http://127.0.0.1:9999/x", "method": "POST",
                        "timeout_seconds": 30, "pass_mode": "multipart"},
    )
    fn_id = await _make_function(
        agent_id=agent_id, name="default", sort_order=0,
        input_schema=fn_input_schema,
        invoke_config=_default_invoke_config(
            auth_token="fn-secret",
        ),
    )
    assert fn_id is not None  # sanity

    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 数据来源 = function，非 agent
        assert data["input_schema"]["fields"]["line"]["label"] == "FN_LABEL"
        assert data["invoke_config"]["auth"]["token"] == "***redacted***"
    finally:
        _clear_override()


# ── 13. 旧 /invoke 代理到 default function（同上）────────────────────────


@pytest.mark.asyncio
async def test_legacy_invoke_delegates_to_default_function(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, is_reachable=True,
        input_schema=_default_input_schema(with_file=False),
        invoke_config={"endpoint": "http://127.0.0.1:9999/agent",
                        "method": "GET", "timeout_seconds": 30,
                        "pass_mode": "multipart"},
    )
    captured: dict = {}

    async def handler(method, url, **kwargs):
        captured["url"] = url
        return _make_response(200, {"data": [{"id": 1}], "ok": True})

    _override_user(user)
    try:
        fn_id = await _make_function(
            agent_id=agent_id, name="default", sort_order=0,
            invoke_config=_default_invoke_config(
                endpoint="http://127.0.0.1:9999/function", method="GET",
            ),
            input_schema=_default_input_schema(with_file=False),
        )
        assert fn_id is not None  # 确认 default function 已建

        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200, resp.text
        # 调用的是 function 的 endpoint，不是 agent 的
        assert captured["url"] == "http://127.0.0.1:9999/function"
    finally:
        _clear_override()


# ── 14. 旧 /form Deprecation 头 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_form_deprecation_header(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )
    # 兼容代理：必须建 sort_order=0 default function（迁移默认存在）
    await _make_function(
        agent_id=agent_id, name="default", sort_order=0,
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )

    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
        assert "Sunset" in resp.headers
    finally:
        _clear_override()


# ── 15. 旧 /invoke Deprecation 头 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_invoke_deprecation_header(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, is_reachable=True,
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )
    await _make_function(
        agent_id=agent_id, name="default", sort_order=0,
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )

    async def handler(method, url, **kwargs):
        return _make_response(200, {"data": [], "ok": True})

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
    finally:
        _clear_override()


# ── 16. 旧 /files Deprecation 头 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_files_deprecation_header(client: AsyncClient):
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, status="active",
        input_schema=_default_input_schema(with_file=True, file_max_mb=10),
        invoke_config=_default_invoke_config(),
    )
    await _make_function(
        agent_id=agent_id, name="default", sort_order=0,
        input_schema=_default_input_schema(with_file=True, file_max_mb=10),
        invoke_config=_default_invoke_config(),
    )

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
        )
        # 不论成功失败，都应带 Deprecation
        assert resp.headers.get("Deprecation") == "true"
    finally:
        _clear_override()


# ── 17. 旧 /invoke agent 无 function → 404（防空洞）────────────────────────


@pytest.mark.asyncio
async def test_legacy_invoke_no_functions_returns_404(client: AsyncClient):
    """没有任何 function 的 agent 调旧 /invoke → 404。

    迁移后真实场景不可达（每插件必有 default function），本测试只是兜底
    兼容代理的边界路径不能返回 500。
    """
    user, org = await _make_org_user()
    agent_id = await _make_tool_agent(
        org_id=org.id, is_reachable=True,
        input_schema=_default_input_schema(with_file=False),
        invoke_config=_default_invoke_config(),
    )
    # 不建任何 function

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {"line": "L1"}},
        )
        assert resp.status_code == 404
    finally:
        _clear_override()
