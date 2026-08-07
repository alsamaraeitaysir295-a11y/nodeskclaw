"""验证 instance_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.exceptions import ForbiddenError
from app.core.security import get_current_user
from app.main import app
from app.models.cluster import Cluster
from app.models.instance import Instance
from app.models.instance_member import InstanceRole
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import instance_member_service
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    """将 get_current_user 依赖替换为固定用户，绕过真实鉴权走真实 HTTP 端点。"""
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    """测试结束后清理依赖覆盖，避免污染后续测试。"""
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user_instance(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"insttest-org-{suffix}", slug=f"insttest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"insttest-{suffix}@example.com",
            name=f"insttest-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        # 创建者故意设为另一个真实存在但与当前用户无关的账号（instances.created_by 有外键约束，
        # 不能直接填随机 UUID），用于验证"创建者豁免"已被去掉
        other_user = User(
            email=f"insttest-creator-{suffix}@example.com",
            name=f"insttest-creator-{suffix}",
            password_hash="x",
        )
        db.add(other_user)
        await db.flush()
        # Instance.cluster_id/namespace/image_version 为必填字段，需先造一个 Cluster
        cluster = Cluster(name=f"cluster-{suffix}", org_id=org.id, created_by=other_user.id)
        db.add(cluster)
        await db.flush()
        instance = Instance(
            org_id=org.id,
            name=f"inst-{suffix}",
            slug=f"inst-{suffix}",
            cluster_id=cluster.id,
            namespace="default",
            image_version="latest",
            created_by=other_user.id,
            runtime="openclaw",
            status="ready",
        )
        db.add(instance)
        await db.commit()
        await db.refresh(user)
        await db.refresh(instance)
        return user, instance


@pytest.mark.asyncio
async def test_member_can_view_but_not_edit():
    user, instance = await _make_org_user_instance(OrgRole.member)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.viewer, db
        ) is None
        with pytest.raises(ForbiddenError):
            await instance_member_service.check_instance_access(
                instance.id, user, InstanceRole.editor, db
            )


@pytest.mark.asyncio
async def test_operator_can_edit_without_being_creator():
    user, instance = await _make_org_user_instance(OrgRole.operator)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.editor, db
        ) is None
        with pytest.raises(ForbiddenError):
            await instance_member_service.check_instance_access(
                instance.id, user, InstanceRole.admin, db
            )


@pytest.mark.asyncio
async def test_admin_passes_all_levels():
    user, instance = await _make_org_user_instance(OrgRole.admin)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.admin, db
        ) is None


@pytest.mark.asyncio
async def test_get_user_instance_role_returns_editor_for_operator():
    user, instance = await _make_org_user_instance(OrgRole.operator)
    async with TestSessionLocal() as db:
        role = await instance_member_service.get_user_instance_role(instance.id, user, db)
        assert role == InstanceRole.editor


@pytest.mark.asyncio
async def test_get_user_instance_role_returns_viewer_for_member():
    user, instance = await _make_org_user_instance(OrgRole.member)
    async with TestSessionLocal() as db:
        role = await instance_member_service.get_user_instance_role(instance.id, user, db)
        assert role == InstanceRole.viewer


# ── 创建实例门槛：POST /api/v1/deploy 需 operator 及以上 ──────────────
# （真实的 org 自助部署入口在 app/api/portal/deploy.py，不是 app/api/deploy.py——
# 后者挂在 admin_router 下，走的是完全独立的 AdminMembership 超管体系）


@pytest.mark.asyncio
async def test_member_cannot_deploy_instance(client: AsyncClient):
    """member 角色调用真实部署端点应被 require_org_member_role("operator") 拒绝（403）。"""
    user, instance = await _make_org_user_instance(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/deploy",
            json={"name": "member-deploy", "cluster_id": instance.cluster_id},
        )
        assert resp.status_code == 403
        # 注：全局 HTTPException 处理器（app/core/exceptions.py）会把 detail 字典
        # 展平到响应体顶层（code/error_code/message_key/message/data），
        # 不是 FastAPI 默认的 {"detail": {...}} 嵌套结构。
        assert resp.json()["error_code"] == 40315
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_deploy_instance(client: AsyncClient, monkeypatch):
    """operator 角色应通过 require_org_member_role("operator") 门槛，走到真实部署逻辑。

    部署的同步阶段之后会 asyncio.create_task 启动真实 K8s 部署管道，测试环境没有可用
    集群，这里 mock 掉 deploy_service.deploy_instance / execute_deploy_pipeline，只验证
    权限校验层放行（不再是本用例的关注点：K8s 管道本身已有其他测试覆盖）。
    """
    from app.services import deploy_service

    class _FakeCtx:
        instance_id = "fake-instance-id"
        name = "fake-instance"

    async def _fake_deploy_instance(body, user, db, org_id=None):
        return "fake-deploy-id", _FakeCtx()

    async def _fake_execute_deploy_pipeline(ctx):
        return None

    monkeypatch.setattr(deploy_service, "deploy_instance", _fake_deploy_instance)
    monkeypatch.setattr(deploy_service, "execute_deploy_pipeline", _fake_execute_deploy_pipeline)

    user, instance = await _make_org_user_instance(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/deploy",
            json={"name": "operator-deploy", "cluster_id": instance.cluster_id},
        )
        assert resp.status_code == 200, resp.text
    finally:
        _clear_override()
