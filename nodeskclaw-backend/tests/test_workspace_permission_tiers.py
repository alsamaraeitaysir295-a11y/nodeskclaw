"""验证 workspace_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.exceptions import ForbiddenError
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.services import workspace_member_service
from tests.conftest import TestSessionLocal


async def _make_org_user_workspace(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"wstest-org-{suffix}", slug=f"wstest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"wstest-{suffix}@example.com",
            name=f"wstest-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        # 创建者故意设为另一个真实存在但与当前用户无关的账号（workspaces.created_by 有外键约束，
        # 不能直接填随机 UUID），用于验证"创建者豁免"已被去掉
        other_user = User(
            email=f"wstest-creator-{suffix}@example.com",
            name=f"wstest-creator-{suffix}",
            password_hash="x",
        )
        db.add(other_user)
        await db.flush()
        workspace = Workspace(org_id=org.id, name=f"ws-{suffix}", created_by=other_user.id)
        db.add(workspace)
        await db.commit()
        await db.refresh(user)
        await db.refresh(workspace)
        return user, workspace


@pytest.mark.asyncio
async def test_member_can_send_chat():
    user, workspace = await _make_org_user_workspace(OrgRole.member)
    async with TestSessionLocal() as db:
        result = await workspace_member_service.check_workspace_access(
            workspace.id, user, "send_chat", db
        )
        assert result is None  # None 表示放行


@pytest.mark.asyncio
async def test_member_cannot_edit_blackboard():
    user, workspace = await _make_org_user_workspace(OrgRole.member)
    async with TestSessionLocal() as db:
        with pytest.raises(ForbiddenError):
            await workspace_member_service.check_workspace_access(
                workspace.id, user, "edit_blackboard", db
            )


@pytest.mark.asyncio
async def test_operator_can_edit_blackboard_and_manage_agents():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "edit_blackboard", db
        ) is None
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "manage_agents", db
        ) is None


@pytest.mark.asyncio
async def test_operator_cannot_manage_settings():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        with pytest.raises(ForbiddenError):
            await workspace_member_service.check_workspace_access(
                workspace.id, user, "manage_settings", db
            )


@pytest.mark.asyncio
async def test_admin_can_manage_settings_without_being_creator():
    user, workspace = await _make_org_user_workspace(OrgRole.admin)
    async with TestSessionLocal() as db:
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "manage_settings", db
        ) is None


@pytest.mark.asyncio
async def test_get_my_permissions_operator_tier():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        perms = await workspace_member_service.get_my_permissions(workspace.id, user, db)
        assert perms["is_admin"] is False
        assert perms["is_org_admin"] is False
        assert set(perms["permissions"]) == {"send_chat", "edit_blackboard", "manage_agents", "edit_topology"}
