"""验证组织成员管理三端点门槛降为 operator，以及 operator 不能把人设为 admin 的能力上限。"""
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


async def _make_org_with_two_members(actor_role: str, target_role: str = OrgRole.member):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"memtest-org-{suffix}", slug=f"memtest-org-{suffix}")
        db.add(org)
        await db.flush()
        actor = User(
            email=f"actor-{suffix}@example.com", name="actor",
            password_hash="x", current_org_id=org.id,
        )
        target = User(
            email=f"target-{suffix}@example.com", name="target",
            password_hash="x", current_org_id=org.id,
        )
        db.add_all([actor, target])
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=actor.id, role=actor_role))
        target_membership = OrgMembership(org_id=org.id, user_id=target.id, role=target_role)
        db.add(target_membership)
        await db.commit()
        await db.refresh(actor)
        await db.refresh(target)
        await db.refresh(target_membership)
        return org, actor, target, target_membership


@pytest.mark.asyncio
async def test_operator_can_promote_member_to_operator(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/members/{membership.id}", json={"role": "operator"}
        )
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_cannot_promote_to_admin(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/members/{membership.id}", json={"role": "admin"}
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_admin_can_promote_to_admin(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.admin)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/members/{membership.id}", json={"role": "admin"}
        )
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_member_cannot_update_role(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.member)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/members/{membership.id}", json={"role": "operator"}
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_remove_member(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.delete(f"/api/v1/orgs/{org.id}/members/{membership.id}")
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_reset_member_password(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.post(f"/api/v1/orgs/{org.id}/members/{target.id}/reset-password")
        assert resp.status_code == 200
    finally:
        _clear_override()
