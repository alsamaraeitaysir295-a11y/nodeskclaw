"""T6：任务空间 API 主链路（httpx + ASGITransport，覆盖设计 §11 T6 验收）。

拆解的 LLM 调用经 fake chat 注入（后台真实任务因无组织 Key 走 decomposition_failed
路径，恰好覆盖失败回退分支）。
"""
import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.api.missions import router as mission_router
from app.core.deps import get_current_org
from app.main import app
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.models.org_membership import OrgMembership
from app.services.mission import mission_service
from tests.conftest import TestSessionLocal, engine  # noqa: F401
from tests.mission_test_helpers import make_env

from types import SimpleNamespace


@pytest.fixture(autouse=True)
async def _mission_api_env():
    """清 mission 表 + 种 org/user/workspace/成员关系 + 覆盖 org 依赖。"""
    async with TestSessionLocal() as db:
        await db.execute(text(
            "TRUNCATE TABLE mission_events, mission_event_counters, mission_artifacts, "
            "mission_nodes, mission_org_configs, missions CASCADE"
        ))
        await db.commit()

    suffix = uuid.uuid4().hex[:8]
    from app.models.cluster import Cluster
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.workspace import Workspace
    async with TestSessionLocal() as db:
        org = Organization(name=f"api-org-{suffix}", slug=f"api-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"api-user-{suffix}", email=f"api-{suffix}@example.com")
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role="admin"))
        cluster = Cluster(name=f"api-cluster-{suffix}", created_by=user.id)
        db.add(cluster)
        await db.flush()
        ws = Workspace(org_id=org.id, name=f"api-ws-{suffix}", created_by=user.id)
        db.add(ws)
        await db.commit()
        ids = (org.id, user.id, ws.id)

    user_ns = SimpleNamespace(id=ids[1], name=f"api-user-{suffix}")
    org_ns = SimpleNamespace(id=ids[0])
    app.dependency_overrides[get_current_org] = lambda: (user_ns, org_ns)
    yield {"org_id": ids[0], "user_id": ids[1], "ws_id": ids[2], "user": user_ns, "org": org_ns}
    app.dependency_overrides.pop(get_current_org, None)


def _fake_chat(payload: dict):
    async def chat(prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)
    return chat


def _valid_payload(n_nodes: int = 2) -> dict:
    return {
        "mission_title": "API 测试任务",
        "brief": {"goal": "目标", "constraints": [], "acceptance_criteria": ["标准"],
                  "key_decisions": []},
        "is_lightweight_candidate": False,
        "nodes": [
            {"title": f"节点{i}", "description": "d", "acceptance_criteria": "a",
             "capability_tags": ["backend"], "depends_on": [] if i == 0 else [0]}
            for i in range(n_nodes)
        ],
        "escalation": {"l2_rules": ["危险操作"]},
    }


async def _decompose(mission_id: str, n_nodes: int = 2) -> None:
    # 等创建端点拉起的后台"真实拆解"任务先失败回落（无组织 Key），
    # 避免它与 fake 拆解对 draft→decomposing 双写竞态
    await asyncio.sleep(0.3)
    ok = await mission_service.run_decomposition(
        mission_id, chat=_fake_chat(_valid_payload(n_nodes)),
        session_factory=TestSessionLocal,  # 后台任务默认连生产引擎（本机不可达）
    )
    assert ok, "拆解应成功"


# ── 主链路：创建 → 拆解 → 确认 → 执行 ──────────────────────────────────────


async def test_full_lifecycle(client, _mission_api_env):
    ctx = _mission_api_env
    # 1. 创建
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "做一个竞品分析"})
    assert r.status_code == 200, r.text
    mission_id = r.json()["data"]["id"]

    # 后台真实拆解无组织 Key → decomposition_failed 回 draft（失败分支覆盖）
    await asyncio.sleep(0.3)
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "draft"

    # 2. fake chat 拆解 → awaiting_confirm
    await _decompose(mission_id)
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "awaiting_confirm"
        assert m.title == "API 测试任务"
        nodes = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mission_id))).scalars().all()
        assert len(nodes) == 2

    # 3. 详情含覆盖检查
    r = await client.get(f"/api/v1/missions/{mission_id}")
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["status"] == "awaiting_confirm"
    assert "coverage" in detail and len(detail["coverage"]) == 2
    node0 = detail["nodes"][0]

    # 4. 确认（带一处编辑）→ executing
    r = await client.post(f"/api/v1/missions/{mission_id}/confirm", json={
        "node_edits": [{"node_id": node0["id"], "title": "改名后的节点0"}],
    })
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "executing"
        n0 = await db.get(MissionNode, node0["id"])
        assert n0.title == "改名后的节点0"

    # 5. 事件列表 + kinds 过滤 + after_seq 增量
    r = await client.get(f"/api/v1/missions/{mission_id}/events")
    events = r.json()["data"]
    types = [e["event_type"] for e in events]
    assert "mission_created" in types and "decomposition_done" in types
    assert "dag_edited" in types
    r = await client.get(f"/api/v1/missions/{mission_id}/events?kinds=decomposition_done")
    assert all(e["event_type"] == "decomposition_done" for e in r.json()["data"])
    last_seq = events[-1]["seq"]
    r = await client.get(f"/api/v1/missions/{mission_id}/events?after_seq={last_seq}")
    assert r.json()["data"] == []

    # 6. 列表
    r = await client.get(f"/api/v1/workspaces/{ctx['ws_id']}/missions")
    assert any(m["id"] == mission_id for m in r.json()["data"])
    r = await client.get(f"/api/v1/workspaces/{ctx['ws_id']}/missions?status=executing")
    assert all(m["status"] == "executing" for m in r.json()["data"])


# ── 验收 / 打回 ─────────────────────────────────────────────────────────────


async def _to_acceptance(mission_id: str) -> str:
    """直接置 acceptance 态（executing→acceptance 的常规转换属 T8 ingest）。"""
    async with TestSessionLocal() as db:
        await db.execute(update(MissionNode).where(
            MissionNode.mission_id == mission_id).values(status="done"))
        await db.execute(update(Mission).where(
            Mission.id == mission_id).values(status="acceptance"))
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mission_id).limit(1))).scalar_one()
        await db.commit()
        return node.id


async def test_accept_and_reject(client, _mission_api_env):
    ctx = _mission_api_env
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "验收测试"})
    mid = r.json()["data"]["id"]
    await _decompose(mid)
    await client.post(f"/api/v1/missions/{mid}/confirm")
    node_id = await _to_acceptance(mid)

    # 打回：点名节点回 pending，Mission 回 executing
    r = await client.post(f"/api/v1/missions/{mid}/reject", json={
        "reason": "质量不达标", "rejected_nodes": [node_id],
    })
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mid)
        assert m.status == "executing"
        assert (await db.get(MissionNode, node_id)).status == "pending"
        rejected = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mid,
            MissionEvent.event_type == "mission_rejected"))).scalars().all()
        assert len(rejected) == 1

    # 再走验收通过
    await _to_acceptance(mid)
    r = await client.post(f"/api/v1/missions/{mid}/accept")
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mid)).status == "archived"


# ── 取消 / 提问回答 / 重试改派 ─────────────────────────────────────────────


async def test_cancel_skips_unfinished_nodes(client, _mission_api_env):
    ctx = _mission_api_env
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "取消测试"})
    mid = r.json()["data"]["id"]
    await _decompose(mid)
    await client.post(f"/api/v1/missions/{mid}/confirm")
    r = await client.post(f"/api/v1/missions/{mid}/cancel")
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mid)).status == "cancelled"
        nodes = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mid))).scalars().all()
        assert all(n.status == "skipped" for n in nodes)


async def test_answer_l2_unblocks(client, _mission_api_env):
    ctx = _mission_api_env
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "回答测试"})
    mid = r.json()["data"]["id"]
    await _decompose(mid, n_nodes=1)

    # 造一个 L2 阻塞：mission blocked + l2_question 事件
    from app.services.mission.event_service import MissionEventService
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mid)
        m.status = "blocked_question"
        ev = await MissionEventService(db).append(
            mid, org_id=m.org_id, event_type="l2_question",
            actor_type="agent", content="需要确认方案",
        )
        await db.commit()
        event_id = ev.id

    r = await client.post(f"/api/v1/missions/{mid}/questions/{event_id}/answer",
                          json={"answer": "按方案A执行"})
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mid)).status == "executing"
        answered = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mid,
            MissionEvent.event_type == "question_answered"))).scalars().all()
        assert len(answered) == 1 and "方案A" in answered[0].content


async def test_retry_and_reassign(client, _mission_api_env):
    ctx = _mission_api_env
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "改派测试"})
    mid = r.json()["data"]["id"]
    await _decompose(mid)
    await client.post(f"/api/v1/missions/{mid}/confirm")
    async with TestSessionLocal() as db:
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mid).limit(1))).scalar_one()
        node.status = "failed"
        node.attempt_count = 3
        await db.commit()
        node_id = node.id

    r = await client.post(f"/api/v1/missions/{mid}/nodes/{node_id}/retry")
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        n = await db.get(MissionNode, node_id)
        assert n.status == "pending" and n.attempt_count == 0

    # 改派目标是真实 FK，先建一个目标实例
    from app.models.cluster import Cluster
    from app.models.instance import Instance
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        cluster = Cluster(name=f"reassign-cluster-{suffix}", created_by=ctx["user_id"])
        db.add(cluster)
        await db.flush()
        inst = Instance(
            org_id=ctx["org_id"], name="目标员工", slug=f"inst-{suffix}",
            cluster_id=cluster.id, namespace=f"ns-{suffix}", image_version="0.5.0",
            created_by=ctx["user_id"],
        )
        db.add(inst)
        await db.commit()
        target_inst_id = inst.id

    r = await client.post(f"/api/v1/missions/{mid}/nodes/{node_id}/reassign",
                          json={"instance_id": target_inst_id})
    assert r.status_code == 200
    async with TestSessionLocal() as db:
        n = await db.get(MissionNode, node_id)
        assert n.status == "matched" and n.assigned_instance_id == target_inst_id


# ── 产物 ───────────────────────────────────────────────────────────────────


async def test_artifacts_list_and_promote(client, _mission_api_env):
    ctx = _mission_api_env
    r = await client.post(f"/api/v1/workspaces/{ctx['ws_id']}/missions",
                          json={"requirement_text": "产物测试"})
    mid = r.json()["data"]["id"]
    await _decompose(mid, n_nodes=1)
    async with TestSessionLocal() as db:
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mid).limit(1))).scalar_one()
        art = MissionArtifact(
            mission_id=mid, node_id=node.id, org_id=ctx["org_id"],
            name="report.md", kind="report", storage_key=f"missions/x/{mid}/a/report.md",
        )
        db.add(art)
        await db.commit()
        art_id = art.id

    r = await client.get(f"/api/v1/missions/{mid}/artifacts")
    arts = r.json()["data"]
    assert len(arts) == 1 and arts[0]["retention"] == "quarantine"

    r = await client.post(f"/api/v1/missions/{mid}/artifacts/{art_id}/promote")
    assert r.status_code == 200
    r = await client.get(f"/api/v1/missions/{mid}/artifacts")
    assert r.json()["data"][0]["retention"] == "promoted"
    assert r.json()["data"][0]["expires_at"] is None


# ── 权限：非成员 403 ───────────────────────────────────────────────────────


async def test_non_member_forbidden(client, _mission_api_env):
    stranger = SimpleNamespace(id="user-stranger-" + uuid.uuid4().hex[:6], name="路人")
    app.dependency_overrides[get_current_org] = lambda: (
        stranger, SimpleNamespace(id=_mission_api_env["org_id"]))
    try:
        r = await client.post(
            f"/api/v1/workspaces/{_mission_api_env['ws_id']}/missions",
            json={"requirement_text": "x"},
        )
        assert r.status_code == 403
    finally:
        user_ns, org_ns = _mission_api_env["user"], _mission_api_env["org"]
        app.dependency_overrides[get_current_org] = lambda: (user_ns, org_ns)
