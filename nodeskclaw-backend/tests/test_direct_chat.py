"""AI 员工双模式：实例维度直聊 + 创建即装必备基因（需求 2026-08-31）。

覆盖：
- 直聊会话/消息 API（未入空间实例可建会话、读历史；跨 org 拒绝）
- chat 端点的隧道未连接拒绝（SSE 满路径与空间聊天同构，不在单测重复覆盖）
- get_org_required_gene_slugs（组织必备基因清单）
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.deps import get_current_org
from app.main import app
from app.models.cluster import Cluster
from app.models.conversation import Conversation
from app.models.gene import Gene
from app.models.instance import Instance
from app.models.org_required_gene import OrgRequiredGene
from app.models.organization import Organization
from app.models.user import User
from app.services import direct_chat_service as dc_service
from app.services.gene_service import get_org_required_gene_slugs
from tests.conftest import TestSessionLocal


@pytest.fixture(autouse=True)
async def _direct_chat_env():
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"dc-org-{suffix}", slug=f"dc-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"dc-user-{suffix}", email=f"dc-{suffix}@example.com")
        db.add(user)
        await db.flush()
        cluster = Cluster(name=f"dc-cluster-{suffix}", created_by=user.id)
        db.add(cluster)
        await db.flush()
        # 未加入任何空间的实例（直聊模式的对象）
        inst = Instance(
            org_id=org.id, name="独立员工", slug=f"inst-{suffix}",
            cluster_id=cluster.id, namespace=f"ns-{suffix}", image_version="0.5.0",
            created_by=user.id,
        )
        db.add(inst)
        await db.commit()
        ids = (org.id, user.id, inst.id)

    user_ns = SimpleNamespace(id=ids[1], name=f"dc-user-{suffix}")
    org_ns = SimpleNamespace(id=ids[0])
    app.dependency_overrides[get_current_org] = lambda: (user_ns, org_ns)
    yield {"org_id": ids[0], "user_id": ids[1], "inst_id": ids[2]}
    app.dependency_overrides.pop(get_current_org, None)


async def test_direct_conversation_lifecycle(client, _direct_chat_env):
    """未入空间实例：建直聊会话 → 列表可见 → 空历史 → 直接写消息可读回。"""
    inst_id = _direct_chat_env["inst_id"]

    r = await client.post(f"/api/v1/instances/{inst_id}/conversations")
    assert r.status_code == 200, r.text
    conv_id = r.json()["data"]["id"]

    r = await client.get(f"/api/v1/instances/{inst_id}/conversations")
    assert r.status_code == 200
    convs = r.json()["data"]
    assert any(c["id"] == conv_id for c in convs)
    assert all(c["member_node_ids"] and inst_id in c["member_node_ids"] for c in convs)

    r = await client.get(f"/api/v1/instances/{inst_id}/conversations/{conv_id}/messages")
    assert r.status_code == 200
    assert r.json()["data"] == []

    # 服务层直写两条消息（绕过隧道），历史读回且顺序正确
    async with TestSessionLocal() as db:
        from app.services.workspace_message_service import record_message
        await record_message(db, workspace_id=None, sender_type="user",
                             sender_id=_direct_chat_env["user_id"], sender_name="u",
                             content="你好", message_type="private",
                             conversation_id=conv_id)
        await record_message(db, workspace_id=None, sender_type="agent",
                             sender_id=inst_id, sender_name="独立员工",
                             content="我在，请讲", message_type="private",
                             conversation_id=conv_id)
        conv = await db.get(Conversation, conv_id)
        assert conv.workspace_id is None
        assert conv.last_message_preview == "我在，请讲"

    r = await client.get(f"/api/v1/instances/{inst_id}/conversations/{conv_id}/messages")
    msgs = r.json()["data"]
    assert [m["content"] for m in msgs] == ["你好", "我在，请讲"]


async def test_direct_chat_requires_tunnel(client, _direct_chat_env):
    """隧道未连接时 chat 端点明确拒绝（400 agent_connection_missing）。"""
    r = await client.post(
        f"/api/v1/instances/{_direct_chat_env['inst_id']}/chat",
        json={"message": "hi"},
    )
    assert r.status_code == 400
    assert "agent_connection_missing" in r.text


async def test_direct_chat_cross_org_forbidden(client, _direct_chat_env):
    """跨 org 实例不可见（404，不泄露存在性）。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org_b = Organization(name=f"dcx-org-{suffix}", slug=f"dcx-org-{suffix}")
        db.add(org_b)
        await db.flush()
        creator = (await db.execute(select(User).limit(1))).scalar()
        cluster = Cluster(name=f"dcx-c-{suffix}", created_by=creator.id)
        db.add(cluster)
        await db.flush()
        inst_b = Instance(
            org_id=org_b.id, name="别人家的员工", slug=f"instb-{suffix}",
            cluster_id=cluster.id, namespace=f"nsb-{suffix}", image_version="0.5.0",
            created_by=creator.id,
        )
        db.add(inst_b)
        await db.commit()
        inst_b_id = inst_b.id

    r = await client.get(f"/api/v1/instances/{inst_b_id}/conversations")
    assert r.status_code == 404


async def test_get_org_required_gene_slugs(_direct_chat_env):
    """组织必备基因清单：按 OrgRequiredGene join 未删除基因返回 slug。"""
    org_id = _direct_chat_env["org_id"]
    async with TestSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        gene = Gene(
            name=f"必备基因A-{suffix}", slug=f"req-a-{suffix}",
            lineage_group_id=str(uuid.uuid4()), manifest='{"capabilities": []}',
        )
        db.add(gene)
        await db.flush()
        db.add(OrgRequiredGene(org_id=org_id, gene_id=gene.id))
        await db.commit()
        gene_id = gene.id

    async with TestSessionLocal() as db:
        slugs = await get_org_required_gene_slugs(db, org_id)
    assert f"req-a-{suffix}" in slugs

    # 软删除必备关联后不再返回
    async with TestSessionLocal() as db:
        link = (await db.execute(select(OrgRequiredGene).where(
            OrgRequiredGene.gene_id == gene_id))).scalar_one()
        link.soft_delete()
        await db.commit()
    async with TestSessionLocal() as db:
        slugs2 = await get_org_required_gene_slugs(db, org_id)
    assert f"req-a-{suffix}" not in slugs2
