"""验证 create_automation_task 补充的组织归属校验（堵跨组织越权漏洞，非角色提权）。

注：brief 原始样例代码里的 Instance(...) 构造只给了 org_id/name/created_by/runtime/status，
但 Instance.cluster_id/namespace/image_version 是 NOT NULL 字段（见 app/models/instance.py），
直接照抄会在 INSERT 时报 IntegrityError。这里参考 test_instance_permission_tiers.py 里
_make_org_user_instance() 的做法，先造一个 Cluster 再补全这三个必填字段。
"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.cluster import Cluster
from app.models.instance import Instance
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


@pytest.mark.asyncio
async def test_cannot_create_automation_task_for_other_org_instance(client: AsyncClient):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org_a = Organization(name=f"org-a-{suffix}", slug=f"org-a-{suffix}")
        org_b = Organization(name=f"org-b-{suffix}", slug=f"org-b-{suffix}")
        db.add_all([org_a, org_b])
        await db.flush()
        user_a = User(
            email=f"user-a-{suffix}@example.com", name="user-a",
            password_hash="x", current_org_id=org_a.id,
        )
        db.add(user_a)
        await db.flush()
        db.add(OrgMembership(org_id=org_a.id, user_id=user_a.id, role=OrgRole.member))
        # 实例/集群的 created_by、cluster_id 均有外键约束，不能直接填随机 UUID，
        # 需先造一个真实存在的账号 + 集群
        other_user = User(
            email=f"other-user-{suffix}@example.com", name="other-user",
            password_hash="x",
        )
        db.add(other_user)
        await db.flush()
        cluster_b = Cluster(name=f"cluster-b-{suffix}", org_id=org_b.id, created_by=other_user.id)
        db.add(cluster_b)
        await db.flush()
        # 实例属于 org_b，与发起用户所在的 org_a 不同
        instance_b = Instance(
            org_id=org_b.id, name=f"inst-b-{suffix}", slug=f"inst-b-{suffix}",
            cluster_id=cluster_b.id, namespace="default", image_version="latest",
            created_by=other_user.id,
            runtime="openclaw", status="ready",
        )
        db.add(instance_b)
        await db.commit()
        await db.refresh(user_a)
        await db.refresh(instance_b)

    _override_user(user_a)
    try:
        resp = await client.post(
            "/api/v1/automation-tasks",
            json={"instance_id": instance_b.id, "name": "跨组织任务", "prompt": "test"},
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_can_create_automation_task_for_own_org_instance(client: AsyncClient):
    """确认修复没有误伤合法请求：同组织成员仍可正常创建自动化任务。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-own-{suffix}", slug=f"org-own-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-own-{suffix}@example.com", name="user-own",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.member))
        other_user = User(
            email=f"other-user-own-{suffix}@example.com", name="other-user-own",
            password_hash="x",
        )
        db.add(other_user)
        await db.flush()
        cluster = Cluster(name=f"cluster-own-{suffix}", org_id=org.id, created_by=other_user.id)
        db.add(cluster)
        await db.flush()
        instance = Instance(
            org_id=org.id, name=f"inst-own-{suffix}", slug=f"inst-own-{suffix}",
            cluster_id=cluster.id, namespace="default", image_version="latest",
            created_by=other_user.id,
            runtime="openclaw", status="ready",
        )
        db.add(instance)
        await db.commit()
        await db.refresh(user)
        await db.refresh(instance)

    _override_user(user)
    try:
        resp = await client.post(
            "/api/v1/automation-tasks",
            json={"instance_id": instance.id, "name": "同组织任务", "prompt": "test"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        _clear_override()
