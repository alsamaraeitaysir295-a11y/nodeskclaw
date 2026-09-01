"""T8：ingest_service —— 隧道上行全链路（设计 §11 T8 验收）。

验收：mock 实例走完 dispatch→ack→progress→artifact→done；
另覆盖 L2 阻塞、幂等（重复 ack/done）、旧 task_id 丢弃、20MB 拒收。
"""
import base64
import uuid

import pytest
from sqlalchemy import select, text

from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.services.mission.ingest_service import handle_mission_message
from tests.conftest import TestSessionLocal
from tests.mission_test_helpers import add_instance, make_env, make_mission, make_node


@pytest.fixture(autouse=True)
async def _clean_mission_tables():
    async with TestSessionLocal() as db:
        await db.execute(text(
            "TRUNCATE TABLE mission_events, mission_event_counters, mission_artifacts, "
            "mission_nodes, mission_org_configs, missions CASCADE"
        ))
        await db.commit()
    yield


def _msg(task_id: str, msg_type: str, data: dict | None = None) -> dict:
    return {"type": msg_type, "task_id": task_id, "data": data or {}}


async def _dispatched_mission() -> tuple[str, str]:
    """建 executing 任务 + dispatched 节点；返回 (mission_id, task_id)。"""
    org_id, _user_id, ws_id, cluster_id = await make_env("ig")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "执行员工")
    mission_id = await make_mission(org_id=org_id, ws_id=ws_id, user_id=_user_id)
    task_id = str(uuid.uuid4())
    await make_node(mission_id, org_id, status="dispatched",
                    assigned_instance_id=inst_id, dispatched_at=None)
    async with TestSessionLocal() as db:
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mission_id))).scalar_one()
        node.last_task_id = task_id
        await db.commit()
    return mission_id, task_id


async def _handle(payload: dict) -> None:
    await handle_mission_message("inst-any", payload, session_factory=TestSessionLocal)


async def _node(mission_id: str) -> MissionNode:
    async with TestSessionLocal() as db:
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mission_id))).scalar_one()
        return node


async def _events(mission_id: str, event_type: str) -> list:
    async with TestSessionLocal() as db:
        return (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == event_type,
        ))).scalars().all()


# ── 验收主链路：dispatch → ack → progress → artifact → done ────────────────


async def test_full_ingest_chain():
    mission_id, task_id = await _dispatched_mission()

    await _handle(_msg(task_id, "mission.task.ack"))
    node = await _node(mission_id)
    assert node.status == "acked"
    assert len(await _events(mission_id, "task_ack")) == 1

    await _handle(_msg(task_id, "mission.task.progress",
                       {"message": "正在搜集资料", "phase": "research"}))
    node = await _node(mission_id)
    assert node.status == "running"
    assert len(await _events(mission_id, "progress")) == 1

    await _handle(_msg(task_id, "mission.task.report", {"message": "准备撰写初稿"}))
    assert len(await _events(mission_id, "narrative")) == 1

    content = "报告内容".encode()
    await _handle(_msg(task_id, "mission.task.artifact", {
        "name": "report.md", "kind": "report",
        "content_base64": base64.b64encode(content).decode(), "size": len(content),
    }))
    async with TestSessionLocal() as db:
        arts = (await db.execute(select(MissionArtifact).where(
            MissionArtifact.mission_id == mission_id))).scalars().all()
        assert len(arts) == 1
        assert arts[0].retention == "quarantine"
        assert arts[0].size_bytes == len(content)
        assert arts[0].expires_at is not None  # TTL 隔离区
        assert arts[0].storage_key.startswith(f"missions/")
    assert len(await _events(mission_id, "artifact_produced")) == 1

    # done：token 两级累加；单节点任务全部完成 → acceptance
    await _handle(_msg(task_id, "mission.task.done", {
        "summary": "已完成", "token_usage": {"input": 100, "output": 50},
    }))
    node = await _node(mission_id)
    assert node.status == "done"
    assert node.prompt_token_cost == 100 and node.completion_token_cost == 50
    assert node.token_cost == 150
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "acceptance"
        assert m.token_cost == 150 and m.prompt_token_cost == 100
    assert len(await _events(mission_id, "node_done")) == 1
    assert len(await _events(mission_id, "system_note")) == 1  # 进入验收提示


# ── 阻塞 / 幂等 / 边界 ─────────────────────────────────────────────────────


async def test_l2_blocked_and_idempotent_done():
    mission_id, task_id = await _dispatched_mission()
    await _handle(_msg(task_id, "mission.task.ack"))
    await _handle(_msg(task_id, "mission.task.blocked", {
        "reason": "需要确认删除范围",
        "question": {"level": "L2", "message": "要删除生产数据吗？"},
    }))
    node = await _node(mission_id)
    assert node.status == "blocked_question"
    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mission_id)).status == "blocked_question"
    assert len(await _events(mission_id, "l2_question")) == 1

    # L1 不阻塞
    await _handle(_msg(task_id, "mission.task.blocked", {
        "reason": "口径歧义",
        "question": {"level": "L1", "message": "按 A 口径继续", "assumption": "假设 A"},
    }))
    assert len(await _events(mission_id, "l1_question")) == 1

    # done 幂等：重复 done 只记一次事件、token 只累加一次
    for _ in range(2):
        await _handle(_msg(task_id, "mission.task.done", {
            "summary": "完成", "token_usage": {"input": 10, "output": 5},
        }))
    node = await _node(mission_id)
    assert node.token_cost == 15
    assert len(await _events(mission_id, "node_done")) == 1


async def test_duplicate_ack_idempotent():
    mission_id, task_id = await _dispatched_mission()
    await _handle(_msg(task_id, "mission.task.ack"))
    await _handle(_msg(task_id, "mission.task.ack"))  # 重复 ack
    assert (await _node(mission_id)).status == "acked"
    assert len(await _events(mission_id, "task_ack")) == 1


async def test_stale_task_id_dropped():
    await _dispatched_mission()
    await _handle(_msg(str(uuid.uuid4()), "mission.task.done", {"summary": "旧消息"}))
    # 无异常、无事件即通过


async def test_oversized_artifact_rejected():
    mission_id, task_id = await _dispatched_mission()
    await _handle(_msg(task_id, "mission.task.artifact", {
        "name": "big.bin", "kind": "file",
        "content_base64": "QQ==", "size": 21 * 1024 * 1024,  # 超 20MB
    }))
    async with TestSessionLocal() as db:
        arts = (await db.execute(select(MissionArtifact).where(
            MissionArtifact.mission_id == mission_id))).scalars().all()
        assert arts == []
