"""验证 instance_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest

from app.core.exceptions import ForbiddenError
from app.models.cluster import Cluster
from app.models.instance import Instance
from app.models.instance_member import InstanceRole
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import instance_member_service
from tests.conftest import TestSessionLocal


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
