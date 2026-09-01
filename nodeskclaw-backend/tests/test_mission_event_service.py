"""T2：MissionEventService —— 计数行 seq 生成与并发安全。

验收（设计 §11 T2）：并发 100 次写事件无 seq 冲突。
测试范围：纯 ORM/服务层，运行依赖 conftest 提供的 TestSessionLocal（真 PostgreSQL）。
"""
import asyncio
import uuid

from sqlalchemy import func, select

from app.models.mission import Mission
from app.models.mission_event import MissionEvent, MissionEventCounter
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.services.mission.event_service import MissionEventService
from tests.conftest import TestSessionLocal


async def _make_mission() -> tuple[str, str]:
    """建组织+用户+空间+Mission（不建计数行）；返回 (mission_id, org_id)。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"ev-org-{suffix}", slug=f"ev-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"ev-user-{suffix}", email=f"ev-{suffix}@example.com")
        db.add(user)
        await db.flush()
        ws = Workspace(org_id=org.id, name=f"ev-ws-{suffix}", created_by=user.id)
        db.add(ws)
        await db.flush()
        m = Mission(
            org_id=org.id,
            workspace_id=ws.id,
            title="事件流测试任务",
            requirement_text="测试 MissionEventService",
            created_by=user.id,
        )
        db.add(m)
        await db.commit()
        return m.id, org.id


async def _append_one(mission_id: str, org_id: str, content: str) -> int:
    """独立会话写一条事件并提交（并发压测的单元）。"""
    async with TestSessionLocal() as db:
        svc = MissionEventService(db)
        ev = await svc.append(
            mission_id,
            org_id=org_id,
            event_type="progress",
            actor_type="agent",
            content=content,
        )
        seq = ev.seq
        await db.commit()
        return seq


# ── 基础：顺序写入 seq 单调递增 ─────────────────────────────────────────────


async def test_append_assigns_monotonic_seqs_and_persists_fields():
    """顺序 append 三条：seq=1,2,3；字段 round-trip 一致；计数行同步创建。"""
    mission_id, org_id = await _make_mission()
    async with TestSessionLocal() as db:
        svc = MissionEventService(db)
        await svc.append(
            mission_id, org_id=org_id,
            event_type="mission_created", actor_type="user",
            actor_name="张三", content="任务创建",
        )
        await svc.append(
            mission_id, org_id=org_id,
            event_type="decomposition_done", actor_type="system",
            node_id=None, payload={"nodes": 3},
        )
        ev3 = await svc.append(
            mission_id, org_id=org_id,
            event_type="progress", actor_type="agent",
            visibility="timeline", content="正在执行",
        )
        await db.commit()

        assert ev3.seq == 3
        rows = (await db.execute(
            select(MissionEvent)
            .where(MissionEvent.mission_id == mission_id)
            .order_by(MissionEvent.seq)
        )).scalars().all()
        assert [r.seq for r in rows] == [1, 2, 3]
        assert rows[0].actor_name == "张三"
        assert rows[1].payload == {"nodes": 3}
        assert rows[2].visibility == "timeline"
        counter = (await db.execute(
            select(MissionEventCounter).where(MissionEventCounter.mission_id == mission_id)
        )).scalar_one()
        assert counter.next_seq == 3


async def test_append_creates_counter_when_missing():
    """Mission 未建计数行（非标准路径创建）时 append 自动补建，seq 从 1 开始。"""
    mission_id, org_id = await _make_mission()
    async with TestSessionLocal() as db:
        # 确认无计数行
        exists = (await db.execute(
            select(MissionEventCounter).where(MissionEventCounter.mission_id == mission_id)
        )).scalar_one_or_none()
        assert exists is None

        svc = MissionEventService(db)
        ev = await svc.append(
            mission_id, org_id=org_id,
            event_type="system_note", actor_type="system", content="补路径",
        )
        await db.commit()
        assert ev.seq == 1


async def test_two_missions_have_independent_counters():
    """两个 Mission 的计数行互不影响，各自从 1 开始。"""
    m1, org1 = await _make_mission()
    m2, _ = await _make_mission()
    async with TestSessionLocal() as db:
        svc = MissionEventService(db)
        await svc.append(m1, org_id=org1, event_type="narrative", actor_type="agent")
        await svc.append(m1, org_id=org1, event_type="narrative", actor_type="agent")
        await svc.append(m2, org_id=org1, event_type="narrative", actor_type="agent")
        await db.commit()
        seqs = dict((await db.execute(
            select(MissionEvent.mission_id, func.max(MissionEvent.seq))
            .group_by(MissionEvent.mission_id)
        )).all())
        assert seqs[m1] == 2
        assert seqs[m2] == 1


# ── T2 验收：并发 100 次写事件无 seq 冲突 ──────────────────────────────────


async def test_concurrent_100_appends_no_seq_conflict():
    """100 个并发任务各开独立会话 append+commit：全部成功，seq 恰为 1..100。"""
    mission_id, org_id = await _make_mission()
    seqs = await asyncio.gather(*[
        _append_one(mission_id, org_id, f"msg-{i}") for i in range(100)
    ])

    assert len(seqs) == 100
    assert sorted(seqs) == list(range(1, 101)), "并发取号必须无重复且覆盖 1..100"

    async with TestSessionLocal() as db:
        count, max_seq = (await db.execute(
            select(func.count(), func.max(MissionEvent.seq))
            .where(MissionEvent.mission_id == mission_id)
        )).one()
        assert count == 100
        assert max_seq == 100
        counter = (await db.execute(
            select(MissionEventCounter).where(MissionEventCounter.mission_id == mission_id)
        )).scalar_one()
        assert counter.next_seq == 100
