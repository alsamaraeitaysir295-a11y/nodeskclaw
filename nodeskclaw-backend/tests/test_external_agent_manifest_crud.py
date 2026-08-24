"""验证扩展后的 create / update / probe 端点对 Manifest 插件化字段的处理。

覆盖范围：外部智能体插件化接入 Phase 1 §6.1（扩展 CRUD + /probe）。
见 ee/docs/外部智能体一期方案.md。
"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(role: str):
    """预置组织 SSRF 白名单为内网段，与本测试用例中使用的 10.50.54.233 / 127.0.0.1 兼容。

    SSRF 防护本身由 test_external_agent_ssrf.py 覆盖，本文件只关注 Manifest CRUD 链路。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"crud-org-{suffix}", slug=f"crud-org-{suffix}",
            external_agent_allowed_cidrs=["10.0.0.0/8", "127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"crud-{suffix}@example.com", name=f"crud-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user


VALID_TOOL_BODY = {
    "name": "热压缺陷查询",
    "type": "tool",
    "invoke_config": {
        "endpoint": "http://10.50.54.233:9000/api/v1/defects/query",
        "method": "POST",
        "auth": {"type": "api_key_header", "header_name": "X-API-Key", "token": "secret"},
        "timeout_seconds": 30,
        "pass_mode": "multipart",
    },
    "input_schema": {
        "order": ["line"],
        "fields": {
            "line": {"type": "string", "ui": "select", "label": "产线",
                      "required": True, "options": ["热压1线"]},
        },
    },
    "output_hint": {"display": "table", "items_path": "data"},
}


@pytest.mark.asyncio
async def test_create_tool_agent_persists_manifest_and_encrypts_token(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/external-agents", json=VALID_TOOL_BODY)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["type"] == "tool"
        assert data["version"] == 1
        assert data["input_schema"]["fields"]["line"]["required"] is True
        # token 应被加密（不再返回明文）
        assert data["invoke_config"]["auth"]["token"] != "secret"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_create_tool_agent_invalid_manifest_returns_400(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        # 故意缺少 invoke_config（tool 型必须项）
        bad = {**VALID_TOOL_BODY}
        bad["invoke_config"] = None
        resp = await client.post("/api/v1/external-agents", json=bad)
        assert resp.status_code == 400
        assert resp.json()["message_key"] == "errors.external_agent.manifest_invalid"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_update_input_schema_bumps_version(client: AsyncClient):
    """input_schema 变更时 version 自动 +1（spec §10）。"""
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=VALID_TOOL_BODY)
        agent_id = create_resp.json()["data"]["id"]
        assert create_resp.json()["data"]["version"] == 1

        updated_schema = {
            "order": ["line", "note"],
            "fields": {
                "line": {"type": "string", "ui": "select", "label": "产线",
                          "required": True, "options": ["热压1线", "热压2线"]},
                "note": {"type": "string", "ui": "input", "label": "备注"},
            },
        }
        patch_resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}",
            json={"input_schema": updated_schema},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["data"]["version"] == 2
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_update_without_manifest_change_does_not_bump_version(client: AsyncClient):
    """未触及 input_schema/invoke_config 的更新不应触发 version+1。"""
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=VALID_TOOL_BODY)
        agent_id = create_resp.json()["data"]["id"]

        patch_resp = await client.patch(
            f"/api/v1/external-agents/{agent_id}",
            json={"description": "新描述"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["data"]["version"] == 1
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_probe_unreachable_tool_agent_marks_not_reachable(client: AsyncClient):
    """POST /{id}/probe 不可达时 is_reachable=false 并写 last_probe。"""
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        body = {**VALID_TOOL_BODY, "invoke_config": {
            **VALID_TOOL_BODY["invoke_config"],
            "endpoint": "http://127.0.0.1:1",
        }}
        create_resp = await client.post("/api/v1/external-agents", json=body)
        agent_id = create_resp.json()["data"]["id"]

        probe_resp = await client.post(f"/api/v1/external-agents/{agent_id}/probe")
        assert probe_resp.status_code == 200, probe_resp.text
        data = probe_resp.json()["data"]
        assert data["reachable"] is False
        assert data["ok"] is False

        # 重新 GET agent 应看到 is_reachable=false + last_probe 已写入
        list_resp = await client.get("/api/v1/external-agents")
        agent = next(a for a in list_resp.json()["data"] if a["id"] == agent_id)
        assert agent["is_reachable"] is False
        assert agent["last_probe"]["ok"] is False
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_probe_requires_operator_role(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        create_resp = await client.post(
            "/api/v1/external-agents",
            json={**VALID_TOOL_BODY, "name": "x"},
        )
        agent_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    member = await _make_org_user(OrgRole.member)
    _override_user(member)
    try:
        resp = await client.post(f"/api/v1/external-agents/{agent_id}/probe")
        assert resp.status_code == 403
    finally:
        _clear_override()
