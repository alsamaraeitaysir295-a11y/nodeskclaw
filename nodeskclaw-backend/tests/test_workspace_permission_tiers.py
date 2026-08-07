"""验证 workspace_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.exceptions import ForbiddenError
from app.core.security import get_current_user
from app.main import app
from app.models.cluster import Cluster
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.services import workspace_member_service
from app.startup.seed_rbac import seed_rbac
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    """将 get_current_user 依赖替换为固定用户，绕过真实鉴权走真实 HTTP 端点。"""
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    """测试结束后清理依赖覆盖，避免污染后续测试。"""
    app.dependency_overrides.pop(get_current_user, None)


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


# ── 创建工作区门槛：POST /api/v1/workspaces 需 operator 及以上 ──────────

async def _create_cluster_for(user: User) -> Cluster:
    """为 WorkspaceCreate 必填的 cluster_id 造一个归属同组织的真实 Cluster 行。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        cluster = Cluster(
            name=f"cluster-{suffix}",
            org_id=user.current_org_id,
            created_by=user.id,
        )
        db.add(cluster)
        await db.commit()
        await db.refresh(cluster)
        return cluster


@pytest.mark.asyncio
async def test_member_cannot_create_workspace(client: AsyncClient):
    """member 角色调用真实创建工作区端点应被 require_org_member_role("operator") 拒绝（403）。

    创建成功路径需经 rbac_sync.grant_role(role_key="workspace_owner")，该内置角色由
    app.startup.seed_rbac 写入而非建表自带，这里先 seed 一遍 + 造一个合法 cluster_id，
    确保请求体本身是完全合法的——如果门槛校验没生效，member 应该能一路创建成功（200），
    而不是被 cluster_id 缺失之类的参数校验（422）意外挡住，导致误判校验已生效。
    """
    await seed_rbac(TestSessionLocal)
    user, _ = await _make_org_user_workspace(OrgRole.member)
    cluster = await _create_cluster_for(user)
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "member-created-ws", "cluster_id": cluster.id},
        )
        assert resp.status_code == 403
        # 注：全局 HTTPException 处理器（app/core/exceptions.py）会把 detail 字典
        # 展平到响应体顶层（code/error_code/message_key/message/data），
        # 不是 FastAPI 默认的 {"detail": {...}} 嵌套结构。
        assert resp.json()["error_code"] == 40315
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_create_workspace(client: AsyncClient):
    """operator 角色应能通过门槛校验，走到真实创建逻辑并成功建组（需要一个归属同组织的 Cluster）。"""
    await seed_rbac(TestSessionLocal)
    user, _ = await _make_org_user_workspace(OrgRole.operator)
    cluster = await _create_cluster_for(user)
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "operator-created-ws", "cluster_id": cluster.id},
        )
        assert resp.status_code == 200
    finally:
        _clear_override()
