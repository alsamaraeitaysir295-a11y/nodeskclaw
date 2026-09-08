"""回归测试：AI 员工绑定外挂知识库的 HTTP 层（POST/GET 响应含嵌套 kb）。

背景（2026-09-08 线上 500）：InstanceKnowledgeBase.kb 关系为 lazy="noload"，
binding.kb 恒为 None，InstanceKnowledgeBaseResponse.model_validate 在
POST /instances/{id}/knowledge-bases 与 GET 列表端点上必然抛 pydantic 校验错。
本测试走真实 HTTP 端点（response_model 校验只在路由层触发），锁定该回归。
"""

import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.cluster import Cluster
from app.models.instance import Instance
from app.models.instance_knowledge_base import InstanceKnowledgeBase
from app.models.knowledge_base import KnowledgeBase
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_instance_with_kb():
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"kbatt-org-{suffix}", slug=f"kbatt-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"kbatt-{suffix}@example.com",
            name=f"kbatt-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.admin))
        cluster = Cluster(name=f"cluster-{suffix}", org_id=org.id, created_by=user.id)
        db.add(cluster)
        await db.flush()
        instance = Instance(
            org_id=org.id,
            name=f"inst-{suffix}",
            slug=f"inst-{suffix}",
            cluster_id=cluster.id,
            namespace="default",
            image_version="latest",
            created_by=user.id,
            runtime="openclaw",
            status="ready",
        )
        db.add(instance)
        await db.flush()
        kb = KnowledgeBase(
            org_id=org.id,
            name=f"kb-{suffix}",
            ragflow_kb_id=f"rf-{suffix}",
            ragflow_endpoint="https://example.com",
            api_key_encrypted="x",
            source_type="ragflow",
            is_reachable=True,
        )
        db.add(kb)
        await db.commit()
        await db.refresh(user)
        await db.refresh(instance)
        await db.refresh(kb)
        return user, instance, kb


@pytest.mark.asyncio
async def test_attach_kb_returns_nested_kb(client: AsyncClient):
    """POST 绑定：响应必须含完整嵌套 kb（此前 500：kb=None 校验失败）。"""
    user, instance, kb = await _make_instance_with_kb()
    _override_user(user)
    try:
        resp = await client.post(
            f"/api/v1/instances/{instance.id}/knowledge-bases",
            json={"kb_id": kb.id},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["kb_id"] == kb.id
        assert data["kb"]["name"] == kb.name
        assert data["kb"]["ragflow_endpoint"] == "https://example.com"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_list_instance_kbs_returns_nested_kb(client: AsyncClient):
    """GET 绑定列表：响应必须含完整嵌套 kb（此前同样 500）。"""
    user, instance, kb = await _make_instance_with_kb()
    async with TestSessionLocal() as db:
        db.add(InstanceKnowledgeBase(
            instance_id=instance.id, kb_id=kb.id,
            enabled=True, created_by=user.id,
        ))
        await db.commit()
    _override_user(user)
    try:
        resp = await client.get(f"/api/v1/instances/{instance.id}/knowledge-bases")
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert len(items) == 1
        assert items[0]["kb"]["name"] == kb.name
    finally:
        _clear_override()
