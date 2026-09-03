"""编排者验收复核（主/子智能体协作）：判定解析、fail-open、打回重派、L2 升级、人工强制验收。"""
import uuid

import pytest
from sqlalchemy import select, text, update

from app.models.mission import Mission
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.services.mission import coordinator_review
from app.services.mission.ingest_service import handle_mission_message
from app.services.mission.mission_service import answer_question
from tests.conftest import TestSessionLocal
from tests.mission_test_helpers import add_instance, make_env, make_mission, make_node

from types import SimpleNamespace


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


async def _dispatched_node() -> tuple[str, str, str]:
    """executing 任务 + dispatched 节点（带 last_task_id）；返回 (mission_id, task_id, node_id)。"""
    org_id, user_id, ws_id, cluster_id = await make_env("rv")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "复核员工")
    mission_id = await make_mission(org_id=org_id, ws_id=ws_id, user_id=user_id)
    task_id = str(uuid.uuid4())
    await make_node(mission_id, org_id, status="dispatched",
                    assigned_instance_id=inst_id, dispatched_at=None)
    async with TestSessionLocal() as db:
        node = (await db.execute(select(MissionNode).where(
            MissionNode.mission_id == mission_id))).scalar_one()
        node.last_task_id = task_id
        await db.commit()
        node_id = node.id
    return mission_id, task_id, node_id


async def _set_node_active(node_id: str):
    """打回后节点回 pending，下一次 done 前需恢复活跃态（模拟调度器重派）。"""
    async with TestSessionLocal() as db:
        await db.execute(update(MissionNode).where(
            MissionNode.id == node_id).values(status="dispatched"))
        await db.commit()


def _fake_review(verdict: bool, feedback: str = "缺少对比表格"):
    async def _review(db, mission, node, summary, *, chat=None):
        return verdict, feedback
    return _review


# ── 判定解析与 fail-open ────────────────────────────────────────────────────


async def test_extract_verdict_parsing():
    assert coordinator_review._extract_verdict('{"verdict": "pass", "feedback": ""}') == (True, "")
    assert coordinator_review._extract_verdict(
        '前置废话 {"verdict": "reject", "feedback": "没有提交产物"} 后置废话') == (False, "没有提交产物")
    # reject 但无 feedback → 视为通过（打回必须有可执行反馈）
    assert coordinator_review._extract_verdict('{"verdict": "reject", "feedback": ""}') == (True, "")
    with pytest.raises(ValueError):
        coordinator_review._extract_verdict("没有任何 JSON")


async def test_review_fail_open_on_llm_error():
    mission_id, _task, node_id = await _dispatched_node()
    async with TestSessionLocal() as db:
        mission = await db.get(Mission, mission_id)
        node = await db.get(MissionNode, node_id)

        async def broken_chat(prompt):
            raise RuntimeError("LLM 不可用")

        passed, feedback = await coordinator_review.review_node_completion(
            db, mission, node, "总结", chat=broken_chat)
    assert passed is True and feedback == ""


# ── 打回 / 升级 / 强制验收 ──────────────────────────────────────────────────


async def test_done_review_reject_redispatches(monkeypatch):
    mission_id, task_id, node_id = await _dispatched_node()
    monkeypatch.setattr(coordinator_review, "review_node_completion", _fake_review(False))

    await handle_mission_message("inst-any",
                                 _msg(task_id, "mission.task.done", {"summary": "进展 2/4"}),
                                 session_factory=TestSessionLocal)

    node = await _get_node(node_id)
    assert node.status == "pending"  # 打回 → 待重派
    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mission_id)).status == "executing"  # 任务不中断
        rejected = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == "node_review_rejected"))).scalars().all()
        assert len(rejected) == 1 and "编排者打回" in rejected[0].content
        # 未产生 node_done（防假完成进入下游）
        dones = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == "node_done"))).scalars().all()
        assert dones == []


async def test_done_review_exhausted_escalates_l2(monkeypatch):
    mission_id, task_id, node_id = await _dispatched_node()
    monkeypatch.setattr(coordinator_review, "review_node_completion", _fake_review(False))

    # 两次打回
    for _ in range(coordinator_review.MAX_REJECTIONS):
        await handle_mission_message("inst-any",
                                     _msg(task_id, "mission.task.done", {"summary": "仍不达标"}),
                                     session_factory=TestSessionLocal)
        await _set_node_active(node_id)

    # 第三次复核未通过 → L2 人工确认
    await handle_mission_message("inst-any",
                                 _msg(task_id, "mission.task.done", {"summary": "最后总结"}),
                                 session_factory=TestSessionLocal)

    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mission_id)).status == "blocked_question"
        node = await db.get(MissionNode, node_id)
        assert node.status == "pending"
        l2 = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == "l2_question"))).scalars().all()
        assert len(l2) == 1
        assert l2[0].payload.get("force_accept") == coordinator_review.FORCE_ACCEPT_KEYWORDS
        assert "最后总结" in l2[0].content


async def test_force_accept_answer_completes_node(monkeypatch):
    mission_id, task_id, node_id = await _dispatched_node()
    monkeypatch.setattr(coordinator_review, "review_node_completion", _fake_review(False))
    for _ in range(coordinator_review.MAX_REJECTIONS + 1):
        await handle_mission_message("inst-any",
                                     _msg(task_id, "mission.task.done", {"summary": "s"}),
                                     session_factory=TestSessionLocal)
        await _set_node_active(node_id)

    async with TestSessionLocal() as db:
        mission = await db.get(Mission, mission_id)
        await db.execute(update(Mission).where(
            Mission.id == mission_id).values(status="blocked_question"))
        await db.commit()
        l2 = (await db.execute(select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == "l2_question"))).scalars().one()

        user = SimpleNamespace(id="user-force", name="验收人")
        await answer_question(db, mission, l2, "同意，按现状通过", user=user)

    async with TestSessionLocal() as db:
        assert (await db.get(Mission, mission_id)).status == "executing"
        node = await db.get(MissionNode, node_id)
        assert node.status == "done"
        dones = (await db.execute(select(MissionEvent).where(
            MissionEvent.node_id == node_id,
            MissionEvent.event_type == "node_done"))).scalars().all()
        assert len(dones) == 1 and "人工验收通过" in dones[0].content


async def test_collect_review_feedback_includes_human_answer(monkeypatch):
    mission_id, task_id, node_id = await _dispatched_node()
    monkeypatch.setattr(coordinator_review, "review_node_completion", _fake_review(False, "缺数据来源"))
    await handle_mission_message("inst-any",
                                 _msg(task_id, "mission.task.done", {"summary": "s"}),
                                 session_factory=TestSessionLocal)

    async with TestSessionLocal() as db:
        feedback = await coordinator_review.collect_review_feedback(db, node_id, mission_id)
    assert feedback == ["编排者打回：缺数据来源"]


async def _get_node(node_id: str) -> MissionNode:
    async with TestSessionLocal() as db:
        return await db.get(MissionNode, node_id)
