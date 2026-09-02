"""T5：调度器 v1 —— CAS 派发 / 实例串行 / 超时看护 / 依赖门 / token 保险丝。

验收（设计 §11 T5）：ack 超时重试、串行派发、done 唤醒下游均有单测；
双副本安全由 CAS + 实例行锁机制保证（本套测其行为语义）。
发送走注入的 FakeSender（mock 隧道）。
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import select, text

from app.models.gene import Gene
from app.models.mission import Mission
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.models.mission_org_config import MissionOrgConfig
from app.services.mission.scheduler import MissionScheduler
from tests.conftest import TestSessionLocal
from tests.mission_test_helpers import (
    add_instance,
    install_gene,
    make_env,
    make_mission,
    make_node,
    utc_past,
)


@pytest.fixture(autouse=True)
async def _clean_mission_tables():
    """清空 mission 表：调度器按全局状态扫描（dispatched 超时/running 停滞），
    历史残留会被本轮捞起，测试必须从干净状态出发（conftest drop_all 不可靠）。
    仅截断 mission 域表，不动其他套件的共享数据。"""
    async with TestSessionLocal() as db:
        await db.execute(text(
            "TRUNCATE TABLE mission_events, mission_event_counters, mission_artifacts, "
            "mission_nodes, mission_org_configs, missions CASCADE"
        ))
        await db.commit()
    yield


class FakeSender:
    """记录调用的 mock 发送器。"""

    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []

    async def __call__(self, instance_id: str, package: dict, task_id: str) -> bool:
        self.calls.append((instance_id, package, task_id))
        return True


class FakeTunnel:
    def __init__(self, *instance_ids: str):
        self.connected_instances = set(instance_ids)


def _scheduler(inst_ids: list[str], sender: FakeSender, **kwargs) -> MissionScheduler:
    return MissionScheduler(
        TestSessionLocal, tunnel=FakeTunnel(*inst_ids), sender=sender, **kwargs,
    )


async def _drain_sends() -> None:
    """run_once 的发送是 fire-and-forget 任务，等它落地。"""
    for _ in range(10):
        await asyncio.sleep(0.02)


async def _get_node(node_id: str) -> MissionNode:
    async with TestSessionLocal() as db:
        node = await db.get(MissionNode, node_id)
        assert node is not None
        return node


async def _events(node_id: str, event_type: str) -> list:
    async with TestSessionLocal() as db:
        return (await db.execute(
            select(MissionEvent).where(
                MissionEvent.node_id == node_id, MissionEvent.event_type == event_type,
            )
        )).scalars().all()


# ── 新派发 ─────────────────────────────────────────────────────────────────


async def test_dispatch_matched_node():
    """matched 节点经行锁+占用检查+CAS 后派发：状态/dispatched 事件/任务包齐全。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "后端员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, status="matched", assigned_instance_id=inst_id)

    sender = FakeSender()
    stats = await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    assert stats["dispatched"] == 1
    node = await _get_node(node_id)
    assert node.status == "dispatched"
    assert node.last_task_id is not None
    assert node.attempt_count == 0
    assert len(sender.calls) == 1
    sent_inst, package, task_id = sender.calls[0]
    assert sent_inst == inst_id
    assert task_id == node.last_task_id
    assert package["session_key"] == node.session_key
    assert package["subtask"]["title"] == "节点"
    assert len(await _events(node_id, "dispatched")) == 1


async def test_instance_occupied_blocks_second_node():
    """实例已有 dispatched 节点（D9 占用）时，同实例另一 matched 节点不派发。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "忙碌员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    await make_node(mission_id, org_id, seq=0, status="dispatched",
                    assigned_instance_id=inst_id, dispatched_at=utc_past(5))
    node_b = await make_node(mission_id, org_id, seq=1, status="matched",
                             assigned_instance_id=inst_id)

    sender = FakeSender()
    stats = await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    assert stats["dispatched"] == 0
    assert sender.calls == []  # 队首占用即跳过，无发送
    assert (await _get_node(node_b)).status == "matched"


async def test_offline_instance_not_dispatched():
    """实例隧道离线：matched 节点原地等待，不产生发送。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "离线员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, status="matched", assigned_instance_id=inst_id)

    sender = FakeSender()
    await _scheduler([], sender).run_once()  # 隧道无连接
    await _drain_sends()
    assert sender.calls == []
    assert (await _get_node(node_id)).status == "matched"


# ── 依赖门：done 唤醒下游 / 未完成不下发 ────────────────────────────────────


async def test_dependency_gate_at_dispatch():
    """上游 done 才派发；上游 running 的 matched 节点防御性拦下（派发步复核）。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_a = await add_instance(org_id, ws_id, cluster_id, "员工A")
    inst_b = await add_instance(org_id, ws_id, cluster_id, "员工B")

    m1 = await make_mission(org_id, ws_id, user_id, title="依赖已就绪")
    await make_node(m1, org_id, seq=0, status="done", assigned_instance_id=inst_a)
    node_a = await make_node(m1, org_id, seq=1, status="matched",
                             assigned_instance_id=inst_a, depends_on=[0])

    m2 = await make_mission(org_id, ws_id, user_id, title="依赖未就绪")
    # 上游 running 但不占 inst_b（不指派），隔离"实例占用"与"依赖门"两个变量
    await make_node(m2, org_id, seq=0, status="running", started_at=utc_past(10))
    node_b = await make_node(m2, org_id, seq=1, status="matched",
                             assigned_instance_id=inst_b, depends_on=[0])

    sender = FakeSender()
    await _scheduler([inst_a, inst_b], sender).run_once()
    await _drain_sends()

    assert (await _get_node(node_a)).status == "dispatched"
    assert (await _get_node(node_b)).status == "matched"


async def test_gap_emits_installable_suggestion():
    """缺失能力建议（产品化缺口处理）：有覆盖基因→推荐安装；缺口不变→不刷屏。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, tags=["payment"])  # 空间无员工 → 必缺口

    # 基因库放一个带 payment 能力的基因（未安装到任何实例，仅供建议检索）
    suffix = uuid.uuid4().hex[:8]
    gene_name = f"支付通道基因-{suffix}"
    async with TestSessionLocal() as db:
        db.add(Gene(
            name=gene_name, slug=f"payment-gene-{suffix}",
            lineage_group_id=str(uuid.uuid4()),
            manifest=json.dumps({"capabilities": ["payment"]}),
        ))
        await db.commit()

    sender = FakeSender()
    sched = _scheduler([], sender)  # 隧道无连接，不影响匹配步
    await sched.run_once()

    notes = await _events(node_id, "system_note")
    assert len(notes) == 1
    assert "建议" in notes[0].content
    assert notes[0].payload["capability_gap"] == ["payment"]
    # 建议列表非空即可（共享测试库里有历史残留的 payment 基因，不锁定具体名）
    assert len(notes[0].payload["suggested_genes"]) >= 1
    node = await _get_node(node_id)
    assert node.status == "pending"  # 缺口不派发，等能力补齐

    # 第二轮缺口集合不变 → 不重复发建议
    await sched.run_once()
    assert len(await _events(node_id, "system_note")) == 1


async def test_match_step_gates_on_dependency():
    """匹配步依赖门：上游未 done 的 pending 节点不进入匹配（match_reason 不动）。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "员工")
    await install_gene(inst_id, "backend-gene", {"capabilities": ["backend"]})

    mission_id = await make_mission(org_id, ws_id, user_id)
    await make_node(mission_id, org_id, seq=0, status="pending", tags=["backend"])
    blocked_id = await make_node(mission_id, org_id, seq=1, status="pending",
                                 tags=["backend"], depends_on=[0])

    sender = FakeSender()
    stats = await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    # seq=0 被匹配并派发；seq=1 依赖未完成保持 pending 且未匹配
    assert stats["matched"] == 1
    blocked = await _get_node(blocked_id)
    assert blocked.status == "pending"
    assert blocked.match_reason is None


async def test_match_step_assigns_from_roster():
    """pending + 依赖就绪 → 匹配器选人 → matched 并派发（done 唤醒下游全链路）。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "后端员工")
    await install_gene(inst_id, "backend-gene", {"capabilities": ["backend"]})

    mission_id = await make_mission(org_id, ws_id, user_id)
    await make_node(mission_id, org_id, seq=0, status="done", assigned_instance_id=inst_id)
    down_id = await make_node(mission_id, org_id, seq=1, status="pending",
                              tags=["backend"], depends_on=[0])

    sender = FakeSender()
    stats = await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    assert stats["matched"] == 1
    assert stats["dispatched"] == 1
    node = await _get_node(down_id)
    assert node.status == "dispatched"
    assert node.assigned_instance_id == inst_id
    assert node.match_reason is not None and node.match_reason.get("chosen") == inst_id


# ── ack 超时看护 ────────────────────────────────────────────────────────────


async def test_ack_timeout_resends_same_task_id():
    """dispatched 超 60s 无 ack：attempt+1 重发，task_id 不变（幂等），计时刷新。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "慢员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, status="dispatched",
                              assigned_instance_id=inst_id,
                              dispatched_at=utc_past(120), attempt_count=0)

    sender = FakeSender()
    await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    node = await _get_node(node_id)
    assert node.status == "dispatched"
    assert node.attempt_count == 1
    assert node.dispatched_at > utc_past(30)  # 计时已刷新
    assert len(sender.calls) == 1
    assert sender.calls[0][2] == node.last_task_id  # 重发用同一幂等 task_id


async def test_ack_exhausted_fails_with_l2():
    """attempt 达到 max_attempts：节点 failed + l2_question（改派/取消入口）。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "失联员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, status="dispatched",
                              assigned_instance_id=inst_id,
                              dispatched_at=utc_past(300), attempt_count=3)

    sender = FakeSender()
    stats = await _scheduler([inst_id], sender).run_once()
    await _drain_sends()

    assert stats["failed"] == 1
    assert (await _get_node(node_id)).status == "failed"
    assert len(await _events(node_id, "l2_question")) == 1
    assert sender.calls == []  # 已达上限不再重发


# ── running 停滞看护 ───────────────────────────────────────────────────────


async def test_running_stall_raises_l2_once():
    """running 长时间无事件 → l2_question（不自动 fail）；未回答前不重复提问。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    inst_id = await add_instance(org_id, ws_id, cluster_id, "停滞员工")
    mission_id = await make_mission(org_id, ws_id, user_id)
    node_id = await make_node(mission_id, org_id, status="running",
                              assigned_instance_id=inst_id, started_at=utc_past(120))

    sender = FakeSender()
    sched = _scheduler([inst_id], sender, running_stall_s=60)
    stats = await sched.run_once()
    assert stats["stalled"] == 1
    assert len(await _events(node_id, "l2_question")) == 1
    assert (await _get_node(node_id)).status == "running"  # 不自动 fail

    stats2 = await sched.run_once()  # 未回答 → 不重复
    assert stats2["stalled"] == 0
    assert len(await _events(node_id, "l2_question")) == 1


# ── token 保险丝（D12）────────────────────────────────────────────────────

async def test_token_fuse_staircase_monitoring():
    """P2 硬阻断：150>100 触发挂起；确认后阈值翻倍(200)，150<200 不再触发；
    消耗涨到 250>200 再次挂起；再确认后阈值=400，250<400 不触发。"""
    org_id, user_id, ws_id, cluster_id = await make_env("sch")
    mission_id = await make_mission(org_id, ws_id, user_id, token_cost=150)

    async with TestSessionLocal() as db:
        # 注意挂在本次测试的 org 上（表有历史残留，取"第一个 org"会错挂）
        db.add(MissionOrgConfig(org_id=org_id, mission_token_fuse=100))
        await db.commit()

    sender = FakeSender()
    # 第 1 次：150 >= 100*2^0 → 挂起
    stats = await _scheduler([], sender).run_once()
    assert stats["fused"] == 1
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "blocked_question"
        # 模拟人工确认：恢复 executing + 递增 ack_count（阈值变 100*2^1=200）
        m.status = "executing"
        m.fuse_ack_count = 1
        await db.commit()

    # 第 2 次：150 < 200 → 不触发（确认后继续监控，阈值已抬高）
    stats2 = await _scheduler([], sender).run_once()
    assert stats2["fused"] == 0
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "executing"

    # 消耗涨到 250 >= 200 → 再次挂起（第 2 次触发）
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        m.token_cost = 250
        await db.commit()
    stats3 = await _scheduler([], sender).run_once()
    assert stats3["fused"] == 1
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "blocked_question"
        # 再确认：阈值变 100*2^2=400
        m.status = "executing"
        m.fuse_ack_count = 2
        await db.commit()

    # 250 < 400 → 不触发
    stats4 = await _scheduler([], sender).run_once()
    assert stats4["fused"] == 0
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        assert m.status == "executing"
