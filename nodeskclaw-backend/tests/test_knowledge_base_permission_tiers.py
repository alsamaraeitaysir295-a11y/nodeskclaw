"""验证知识库端点的三级权限门槛（放宽 list 为 member 可读）。"""
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
    """将 get_current_user 依赖替换为固定用户，绕过真实鉴权走真实 HTTP 端点。"""
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    """测试结束后清理依赖覆盖，避免污染后续测试。"""
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"kbtest-org-{suffix}", slug=f"kbtest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"kbtest-{suffix}@example.com", name=f"kbtest-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_member_can_list_kbs(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.get("/api/v1/knowledge-bases")
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_member_cannot_create_kb(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/knowledge-bases", json={
            "name": "kb1", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_create_kb(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/knowledge-bases", json={
            "name": "kb2", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_cannot_delete_kb(client: AsyncClient):
    admin_user = await _make_org_user(OrgRole.admin)
    _override_user(admin_user)
    try:
        create_resp = await client.post("/api/v1/knowledge-bases", json={
            "name": "kb3", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        kb_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    operator = await _make_org_user(OrgRole.operator)
    _override_user(operator)
    try:
        resp = await client.delete(f"/api/v1/knowledge-bases/{kb_id}")
        assert resp.status_code == 403
    finally:
        _clear_override()
