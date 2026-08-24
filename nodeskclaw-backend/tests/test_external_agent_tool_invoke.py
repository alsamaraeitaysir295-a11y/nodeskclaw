"""tool 型插件 /form、/invoke、/files 端点测试（spec §6.2 + §8）。

覆盖：
- /form：active 时返回 input_schema；draft/disabled 时 403；auth.token 脱敏
- /invoke：合法参数 200 + {success,data}；缺必填 422 字段级；上游 5xx → success:false
        且 HTTP 200；不可达 503；pass_mode=multipart 真实发 multipart；
        pass_mode=url_ref 在 body 内含 URL；审计日志被发出
- /files：合法上传、过大拒绝、扩展名拒绝；chat 型 422
"""

from __future__ import annotations

import asyncio
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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
from tests.conftest import TestSessionLocal


# ── helpers ─────────────────────────────────────────────────────────────────


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_with_user(role: str = OrgRole.operator, agent_status: str = "active",
                              is_reachable: bool = True, tool_kind: str = "tool"):
    """建一个组织 + 用户 + 一个 tool 型 Agent，返回 (user, org, agent_id)。

    组织预置 SSRF 白名单为 127.0.0.0/8 —— 本文件测试用的 127.0.0.1:9999 属 loopback，
    与生产 SSRF 防护正交；本测试关注 tool 型 invoke 链路本身。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"tool-org-{suffix}", slug=f"tool-org-{suffix}",
            external_agent_allowed_cidrs=["127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"tool-{suffix}@example.com", name=f"tool-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))

        invoke_config = {
            "endpoint": "http://127.0.0.1:9999/tool",
            "method": "POST",
            "auth": {"type": "bearer", "header_name": None,
                     "token": encrypt_sensitive("test-secret")},
            "timeout_seconds": 30,
            "pass_mode": "multipart",
            "_output_hint": {"display": "table", "items_path": "data",
                             "primary_key": "id"},
        }
        input_schema = {
            "order": ["line", "note", "file"],
            "fields": {
                "line": {"type": "string", "ui": "select", "label": "产线",
                         "required": True, "options": ["L1", "L2"]},
                "note": {"type": "string", "ui": "input", "label": "备注",
                         "required": False},
                "file": {"type": "file", "ui": "upload", "label": "明细",
                         "accept": [".xlsx", ".csv"], "max_mb": 1, "required": False},
            },
        }
        agent = ExternalAgent(
            org_id=org.id,
            name=f"tool-agent-{suffix}",
            endpoint="http://127.0.0.1:9999",
            protocol="openai_compatible",
            type=tool_kind,
            invoke_config=invoke_config,
            input_schema=input_schema,
            status=agent_status,
            is_reachable=is_reachable,
        )
        db.add(agent)
        await db.flush()
        # Phase 2 §7.4：旧 /{id}/form, /{id}/invoke, /{id}/files 内部代理到
        # sort_order=0 default function；为模拟迁移后的存量插件行为，测试
        # 预设一条 default function（与 agent 列存同份数据），避免 404。
        if tool_kind == "tool":
            default_fn = ExternalAgentFunction(
                agent_id=agent.id,
                name="default",
                summary=agent.description,
                invoke_config=dict(agent.invoke_config) if agent.invoke_config else None,
                input_schema=dict(agent.input_schema) if agent.input_schema else None,
                status=agent_status,
                sort_order=0,
                source="manual",
                version=agent.version or 1,
            )
            db.add(default_fn)
        await db.commit()
        await db.refresh(user)
        await db.refresh(agent)
        return user, org, agent.id


def _make_response(status_code: int, json_body=None, content_type: str = "application/json") -> httpx.Response:
    if json_body is not None and content_type == "application/json":
        import json as _json
        content = _json.dumps(json_body).encode("utf-8")
    else:
        content = b"" if json_body is None else (
            json_body if isinstance(json_body, bytes) else str(json_body).encode("utf-8"))
    request = httpx.Request("POST", "http://127.0.0.1:9999")
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"content-type": content_type},
        request=request,
    )


class _FakeAsyncClient:
    """测试用 httpx.AsyncClient 替身：保存每次 send 的 request，可注入 response / exception。

    不继承 httpx.AsyncClient，避免污染全局 httpx（ASGITransport 的 AsyncClient
    会同时使用同一个类，patch httpx.AsyncClient.send 会一并影响它）。改在调用
    现场 patch `app.services.external_agent_tool_service.httpx.AsyncClient` 指向
    本类，触发后把 send 调用转发到预存的 callable。
    """

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        # 兼容我们用 client.request(method, url, ...) 的写法
        return await self._dispatch(method, url, **kwargs)

    async def send(self, request, **kwargs):
        return await self._dispatch(request.method, str(request.url), request=request, **kwargs)

    async def _dispatch(self, method, url, **kwargs):
        handler = getattr(self, "_handler", None)
        if handler is None:
            raise RuntimeError("AsyncClient mock has no handler; use _set_handler()")
        return await handler(method, url, **kwargs)

    def _set_handler(self, handler):
        self._handler = handler


def _patch_tool_http(handler):
    """把 app.services.external_agent_tool_service.httpx.AsyncClient 桩成 _FakeAsyncClient。

    handler: async callable(method, url, **kwargs) -> httpx.Response 或抛异常。
    """
    holder: dict = {}

    def factory(*args, **kwargs):
        c = _FakeAsyncClient(*args, **kwargs)
        c._handler = handler
        holder["client"] = c
        return c

    p = patch("app.services.external_agent_tool_service.httpx.AsyncClient", side_effect=factory)
    return p, holder


# ── /form ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_form_active_tool_returns_input_schema_and_redacts_token(client: AsyncClient):
    user, org, agent_id = await _make_org_with_user(agent_status="active")
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["input_schema"]["fields"]["line"]["required"] is True
        assert data["output_hint"]["items_path"] == "data"
        # invoke_config.auth.token 必须脱敏——这是关键安全约束
        token = data["invoke_config"]["auth"]["token"]
        assert token == "***redacted***"
        # 不应包含明文 token
        assert "test-secret" not in str(data)
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_form_draft_returns_403(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(agent_status="draft")
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.not_active"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_form_disabled_returns_403(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(agent_status="disabled")
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.not_active"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_form_chat_agent_returns_400(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(tool_kind="chat")
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/external-agents/{agent_id}/form")
        assert resp.status_code == 400
    finally:
        _clear_override()


# ── /invoke ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_unreachable_agent_returns_503(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(is_reachable=False)
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {"line": "L1"}},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.invoke_unreachable"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_missing_required_field_returns_422_with_field_errors(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user()
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {"note": "no line"}},  # 缺必填 line
        )
        assert resp.status_code == 422
        body = resp.json()
        # AppException handler 把 field_errors 合并到顶层响应（不是 detail）
        assert body["message_key"] == "errors.external_agent.invoke_validation_error"
        field_errors = body.get("field_errors") or []
        assert any(err.get("field") == "line" for err in field_errors)
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_upstream_500_returns_success_false_with_200(client: AsyncClient):
    """spec §6.2 第 5 步：上游非 2xx → success:false 但 HTTP 仍 200（不要让前端被误判为平台错误）。"""
    user, _, agent_id = await _make_org_with_user()

    resp_500 = _make_response(500, {"detail": "server error"})
    captured_audit: list[dict] = []

    async def fake_emit(event: str, **kwargs):
        if event == "operation_audit":
            captured_audit.append(kwargs)

    async def handler(method, url, **kwargs):
        return resp_500

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patch.object(hooks, "emit", side_effect=fake_emit), patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["success"] is False
        assert data["upstream_status"] == 500
        assert "error" in data
        assert data["message_key"] == "errors.external_agent.invoke_upstream_error"
        # 审计：应有一条 invoked 记录
        assert any(a.get("action") == "external_agent.invoked" for a in captured_audit)
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_success_returns_data_shape(client: AsyncClient):
    """合法参数 + 上游 200 → success:true + data + display + items_path。"""
    user, _, agent_id = await _make_org_with_user()
    upstream_body = {"data": [{"id": 1, "name": "row1"}], "total": 1}
    resp_200 = _make_response(200, upstream_body)

    async def handler(method, url, **kwargs):
        return resp_200

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1", "note": "ok"}},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["success"] is True
        assert data["display"] == "table"
        assert data["items_path"] == "data"
        assert data["data"]["data"][0]["id"] == 1
        assert data["resolved_items"] == [{"id": 1, "name": "row1"}]
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_multipart_pass_mode_actually_sends_multipart(client: AsyncClient):
    """spec §8 默认 multipart：实际请求的 kwargs 里必须含 files= 列表（按 file_id 上传后引用）。"""
    user, _, agent_id = await _make_org_with_user()

    # 上传一个文件并拿 file_id，提交时把它作为 file 字段值传给 invoke
    async def fake_upload(file_content, filename, content_type, org_id):
        return f"external-agent-files/{org_id}/fake/{filename}"

    async def fake_presign(key, expires=3600):
        return f"http://test/api/v1/files/local/{key}?expires=0&sig=fake"

    async def fake_download(key):
        # 文件本体取自 fake，因为 storage 没真的落盘
        return b"xls-mock-body"

    captured: dict = {}

    async def handler(method, url, **kwargs):
        captured["method"] = method
        captured["kwargs"] = kwargs
        return _make_response(200, {"ok": True})

    patcher, _ = _patch_tool_http(handler)

    _override_user(user)
    try:
        with patch.object(storage_service, "upload_external_agent_file",
                          side_effect=fake_upload), \
             patch.object(storage_service, "get_presigned_url",
                          side_effect=fake_presign), \
             patch.object(storage_service, "download_file",
                          side_effect=fake_download), \
             patch.object(storage_service, "download_raw",
                          side_effect=fake_download), \
             patcher:
            upload_resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/files",
                files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
            )
            assert upload_resp.status_code == 200
            file_id = upload_resp.json()["data"]["file_id"]

            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1", "file": file_id}},
            )
        assert resp.status_code == 200, resp.text
        # multipart 模式：handler 收到 files= 列表，且至少含一项
        files = captured["kwargs"].get("files") or []
        assert files, f"expected non-empty files list, got {captured['kwargs']}"
        # 普通字段进 data=
        data = captured["kwargs"].get("data") or {}
        assert data.get("line") == "L1"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_url_ref_pass_mode_sends_url_in_body(client: AsyncClient):
    """spec §8 url_ref 模式：body 内含预签名 URL 引用，不发 multipart。"""
    user, _, agent_id = await _make_org_with_user()
    # 改 pass_mode 为 url_ref（Phase 2 §7.4：旧 /invoke 内部代理到 default function，
    # 因此需要同时改 agent.invoke_config 与 default function.invoke_config）
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        agent = (await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent_id)
        )).scalar_one()
        ic = dict(agent.invoke_config or {})
        ic["pass_mode"] = "url_ref"
        agent.invoke_config = ic
        default_fn = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id,
                ExternalAgentFunction.sort_order == 0,
            )
        )).scalar_one()
        fn_ic = dict(default_fn.invoke_config or {})
        fn_ic["pass_mode"] = "url_ref"
        default_fn.invoke_config = fn_ic
        await db.commit()

    captured: dict = {}

    async def handler(method, url, **kwargs):
        import json as _json
        captured["content_type"] = (kwargs.get("headers") or {}).get("content-type", "")
        captured["kwargs"] = kwargs
        # 拿到 json body 转回 dict 看是否含 URL 引用结构
        json_body = kwargs.get("json")
        if json_body is None and "content" in kwargs:
            try:
                json_body = _json.loads(kwargs["content"])
            except Exception:
                pass
        captured["body"] = json_body
        return _make_response(200, {"ok": True})

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200, resp.text
        # url_ref 模式不发 multipart（无 files=）
        assert not captured["kwargs"].get("files"), "url_ref must not send multipart files"
        # 透传字段 line 出现在 body 里
        body = captured["body"]
        assert body is not None
        assert body.get("line") == "L1"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_audit_log_is_emitted(client: AsyncClient):
    """spec §9.6：invoke 全量记录审计。"""
    user, _, agent_id = await _make_org_with_user()
    captured_audit: list[dict] = []

    async def fake_emit(event: str, **kwargs):
        if event == "operation_audit":
            captured_audit.append(kwargs)

    async def handler(method, url, **kwargs):
        return _make_response(200, {"ok": True})

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patch.object(hooks, "emit", side_effect=fake_emit), patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 200
        invoked = [a for a in captured_audit if a.get("action") == "external_agent.invoked"]
        assert len(invoked) >= 1
        details = invoked[-1]["details"]
        assert details["ok"] is True
        assert details["upstream_status"] == 200
        assert "line" in details["params_summary"]
        assert "latency_ms" in details
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_connect_error_returns_503(client: AsyncClient):
    """spec §6.2 第 1 步：上游不可达 → 503 友好错误（不裸 502）。"""
    user, _, agent_id = await _make_org_with_user()

    async def handler(method, url, **kwargs):
        raise httpx.ConnectError("refused")

    _override_user(user)
    try:
        patcher, _ = _patch_tool_http(handler)
        with patcher:
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {"line": "L1"}},
            )
        assert resp.status_code == 503
        assert resp.json()["message_key"] == "errors.external_agent.invoke_unreachable"
    finally:
        _clear_override()


# ── /files ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_chat_agent_returns_400(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(tool_kind="chat")
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_files_tool_no_file_field_returns_400(client: AsyncClient):
    """无 file 字段的 tool Agent 调用 /files → 400（防误用）。

    Phase 2 §7.4：旧 /files 代理到 default function.input_schema，因此必须同时清空
    agent 与 default function 的 file 字段定义，否则 400 不会触发。
    """
    user, _, agent_id = await _make_org_with_user()
    new_schema = {
        "order": ["line"],
        "fields": {"line": {"type": "string", "ui": "select",
                            "label": "产线", "required": True,
                            "options": ["L1", "L2"]}},
    }
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        agent = (await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent_id)
        )).scalar_one()
        agent.input_schema = new_schema
        default_fn = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id,
                ExternalAgentFunction.sort_order == 0,
            )
        )).scalar_one()
        default_fn.input_schema = new_schema
        await db.commit()

    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_files_rejects_wrong_extension(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user()
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("malware.exe", b"abc", "application/octet-stream")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.file_type_not_allowed"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_files_rejects_too_large(client: AsyncClient):
    """max_mb=1 in schema → 上传 2MB 必拒。"""
    user, _, agent_id = await _make_org_with_user()
    _override_user(user)
    try:
        big = b"x" * (2 * 1024 * 1024)
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("data.xlsx", big, "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.file_too_large"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_files_accepts_valid_file(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user()
    _override_user(user)
    try:
        # 准备：fake 存储后端，把 upload_external_agent_file 与 get_presigned_url 都桩掉
        async def fake_upload(file_content, filename, content_type, org_id):
            return f"external-agent-files/{org_id}/fake/{filename}"

        async def fake_presign(key, expires=3600):
            return f"http://test/api/v1/files/local/{key}?expires=0&sig=fake"

        with patch.object(storage_service, "upload_external_agent_file",
                          side_effect=fake_upload), \
             patch.object(storage_service, "get_presigned_url",
                          side_effect=fake_presign):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/files",
                files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["name"] == "data.xlsx"
        assert data["size"] == 3
        assert data["file_id"].startswith("external-agent-files/")
        assert data["url"].startswith("http://test/api/v1/files/local/")
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_files_disabled_agent_returns_403(client: AsyncClient):
    user, _, agent_id = await _make_org_with_user(agent_status="disabled")
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/files",
            files={"file": ("data.xlsx", b"abc", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.not_active"
    finally:
        _clear_override()
