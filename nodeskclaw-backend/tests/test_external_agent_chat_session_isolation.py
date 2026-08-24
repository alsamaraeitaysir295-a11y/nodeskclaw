"""验证外部 Agent chat 端点的会话归属校验（IDOR 回归测试）。

背景：POST /{agent_id}/chat 曾直接从请求体取 session_id 使用，不校验归属，
知道他人 session_id 的用户可读取/污染他人对话。见
ee/docs/外部智能体一期评审.md P0-B。
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


async def _make_org_user(role: str, org: Organization | None = None):
    """在指定组织下建一个成员；不传 org 时新建一个组织。

    组织预置 SSRF 白名单为 127.0.0.0/8 —— 测试用 127.0.0.1:1 探活，
    与本测试关注的"会话归属校验"链路正交。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        if org is None:
            org = Organization(
                name=f"idor-org-{suffix}", slug=f"idor-org-{suffix}",
                external_agent_allowed_cidrs=["127.0.0.0/8"],
            )
            db.add(org)
            await db.flush()
        user = User(
            email=f"idor-{suffix}@example.com", name=f"idor-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user, org


CREATE_BODY = {
    "name": "idor-test-agent", "endpoint": "https://example.com", "protocol": "openai_compatible",
}


@pytest.mark.asyncio
async def test_chat_with_other_users_session_id_returns_403(client: AsyncClient):
    """用户 A 的 session_id 被用户 B 拿去调用 chat 端点 → 403，不触发外部调用。"""
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=CREATE_BODY)
        assert create_resp.status_code == 200
        agent_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    user_a, _ = await _make_org_user(OrgRole.member, org=org)
    _override_user(user_a)
    try:
        session_resp = await client.post(f"/api/v1/external-agents/{agent_id}/sessions")
        assert session_resp.status_code == 200
        session_id = session_resp.json()["data"]["id"]
    finally:
        _clear_override()

    user_b, _ = await _make_org_user(OrgRole.member, org=org)
    _override_user(user_b)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={"message": "hi", "session_id": session_id},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.session_forbidden"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_nonexistent_session_id_returns_same_403(client: AsyncClient):
    """不存在的 session_id 与"存在但非本人"的 session_id 返回同样的 403，避免探测。"""
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=CREATE_BODY)
        agent_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    user = (await _make_org_user(OrgRole.member, org=org))[0]
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={"message": "hi", "session_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.session_forbidden"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_own_session_id_passes_ownership_check(client: AsyncClient):
    """自己的 session_id 应通过归属校验（后续因外部服务不可达而失败，但不应是 403）。

    endpoint 故意指向本机一个无人监听的端口，让 httpx 立即 ConnectError，
    避免测试依赖真实外网可达性、拖慢或变得不确定。
    """
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        unreachable_body = {**CREATE_BODY, "endpoint": "http://127.0.0.1:1"}
        create_resp = await client.post("/api/v1/external-agents", json=unreachable_body)
        agent_id = create_resp.json()["data"]["id"]
        session_resp = await client.post(f"/api/v1/external-agents/{agent_id}/sessions")
        session_id = session_resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={"message": "hi", "session_id": session_id},
        )
        # StreamingResponse 的状态行在进入生成器前就已确定为 200；外部连接失败
        # 发生在流内部（被转换成一条 SSE error 事件），不应体现为整体 403。
        assert resp.status_code == 200
        assert "session_forbidden" not in resp.text
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_foreign_storage_key_returns_403(client: AsyncClient):
    """attachments[].storage_key 不在当前组织的外部 Agent 附件命名空间内 → 403，
    不会被拿去签发预签名/HMAC 下载 URL（等同任意文件读取）。见
    ee/docs/外部智能体一期评审.md P0-1（storage_key IDOR）。
    """
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=CREATE_BODY)
        agent_id = create_resp.json()["data"]["id"]
        session_resp = await client.post(f"/api/v1/external-agents/{agent_id}/sessions")
        session_id = session_resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={
                "message": "hi",
                "session_id": session_id,
                "attachments": [{
                    "name": "secret.txt",
                    "size": 10,
                    "content_type": "text/plain",
                    "storage_key": "external-agent-files/other-org-id/some-uuid/secret.txt",
                    "url": "https://example.com/x",
                }],
            },
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.attachment_forbidden"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_path_traversal_storage_key_returns_403(client: AsyncClient):
    """storage_key 含 ../ 试图逃逸出附件命名空间 → 403。"""
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        create_resp = await client.post("/api/v1/external-agents", json=CREATE_BODY)
        agent_id = create_resp.json()["data"]["id"]
        session_resp = await client.post(f"/api/v1/external-agents/{agent_id}/sessions")
        session_id = session_resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={
                "message": "hi",
                "session_id": session_id,
                "attachments": [{
                    "name": "etc-passwd",
                    "size": 10,
                    "content_type": "text/plain",
                    "storage_key": f"external-agent-files/{org.id}/../../../../etc/passwd",
                    "url": "https://example.com/x",
                }],
            },
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.attachment_forbidden"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_own_org_storage_key_passes_ownership_check(client: AsyncClient):
    """当前组织自己的外部 Agent 附件命名空间下的 storage_key 应通过校验。"""
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        unreachable_body = {**CREATE_BODY, "endpoint": "http://127.0.0.1:1"}
        create_resp = await client.post("/api/v1/external-agents", json=unreachable_body)
        agent_id = create_resp.json()["data"]["id"]
        session_resp = await client.post(f"/api/v1/external-agents/{agent_id}/sessions")
        session_id = session_resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/chat",
            json={
                "message": "hi",
                "session_id": session_id,
                "attachments": [{
                    "name": "note.txt",
                    "size": 10,
                    "content_type": "text/plain",
                    "storage_key": f"external-agent-files/{org.id}/some-uuid/note.txt",
                    "url": "https://example.com/x",
                }],
            },
        )
        assert resp.status_code == 200
        assert "attachment_forbidden" not in resp.text
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_chat_with_other_agents_session_id_returns_403(client: AsyncClient):
    """同一用户在 Agent A 下的 session_id 被拿去调用 Agent B 的 chat 端点 → 403，
    避免跨 Agent 复用会话（历史消息、附件会被当作不相关 Agent 的上下文发出去）。
    见 review P1-1。
    """
    admin, org = await _make_org_user(OrgRole.admin)
    _override_user(admin)
    try:
        agent_a_resp = await client.post(
            "/api/v1/external-agents",
            json={**CREATE_BODY, "name": "agent-a"},
        )
        agent_a_id = agent_a_resp.json()["data"]["id"]
        agent_b_resp = await client.post(
            "/api/v1/external-agents",
            json={**CREATE_BODY, "name": "agent-b"},
        )
        agent_b_id = agent_b_resp.json()["data"]["id"]

        session_resp = await client.post(f"/api/v1/external-agents/{agent_a_id}/sessions")
        session_id_for_a = session_resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/external-agents/{agent_b_id}/chat",
            json={"message": "hi", "session_id": session_id_for_a},
        )
        assert resp.status_code == 403
        assert resp.json()["message_key"] == "errors.external_agent.session_forbidden"
    finally:
        _clear_override()

