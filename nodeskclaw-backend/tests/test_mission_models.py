"""任务空间（Mission P1）数据模型层测试 — 设计 T1 验收。

覆盖 docs/mission-space-p1-design.md §3（v3）：
- 建表（conftest autouse setup_db 走 Base.metadata.create_all，新模型已注册 __init__.py）
- Mission / MissionNode / MissionArtifact / MissionOrgConfig / MissionEventCounter 字段 round-trip 与默认值
- MissionEvent (mission_id, seq) 唯一（partial unique index，软删后 seq 可复用）
- CapabilityTag (org_id, tag) 唯一（非空 org scope）

测试范围：纯 ORM 层，不依赖 HTTP 端点；运行依赖 conftest 提供的 TestSessionLocal。
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.capability_tag import CapabilityTag
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent, MissionEventCounter
from app.models.mission_node import MissionNode
from app.models.mission_org_config import MissionOrgConfig
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from tests.conftest import TestSessionLocal


async def _make_org_user_workspace() -> tuple[str, str, str]:
    """建组织 + 用户 + 协作空间（Mission 的三个父行）；返回 (org_id, user_id, workspace_id)。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"ms-org-{suffix}", slug=f"ms-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"ms-user-{suffix}", email=f"ms-{suffix}@example.com")
        db.add(user)
        await db.flush()
        ws = Workspace(org_id=org.id, name=f"ms-ws-{suffix}", created_by=user.id)
        db.add(ws)
        await db.commit()
        return org.id, user.id, ws.id


async def _make_mission() -> tuple[str, Mission]:
    """建一个最小 Mission；返回 (mission_id, mission)。"""
    org_id, user_id, ws_id = await _make_org_user_workspace()
    async with TestSessionLocal() as db:
        m = Mission(
            org_id=org_id,
            workspace_id=ws_id,
            title="写一份竞品分析报告",
            requirement_text="帮我分析三个竞品并输出报告",
            created_by=user_id,
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m.id, m


# ── Mission round-trip 与默认值 ─────────────────────────────────────────────


async def test_mission_round_trip_defaults():
    """create → query → 默认值断言（status=draft、type=standard、token 三项=0、brief={}）。"""
    _, m = await _make_mission()
    async with TestSessionLocal() as db:
        found = await db.get(Mission, m.id)
        assert found is not None
        assert found.status == "draft"
        assert found.mission_type == "standard"
        assert found.brief == {}
        assert found.token_cost == 0
        assert found.prompt_token_cost == 0
        assert found.completion_token_cost == 0
        assert found.escalation_policy is None
        assert found.artifact_ttl_days is None
        assert found.fuse_acknowledged_at is None
        assert found.coordinator_meta is None
        assert found.created_at is not None
        assert found.deleted_at is None


# ── MissionNode round-trip 与默认值 ─────────────────────────────────────────


async def test_mission_node_round_trip_defaults():
    """节点默认值：status=pending、attempt 0/3、capability_tags=[]、depends_on=[]。"""
    mission_id, _ = await _make_mission()
    async with TestSessionLocal() as db:
        node = MissionNode(
            mission_id=mission_id,
            org_id=(await db.get(Mission, mission_id)).org_id,
            seq=0,
            title="搜集竞品资料",
            session_key=f"fake-org:{mission_id}:fake-node",
        )
        db.add(node)
        await db.commit()
        await db.refresh(node)

        assert node.status == "pending"
        assert node.capability_tags == []
        assert node.depends_on == []
        assert node.attempt_count == 0
        assert node.max_attempts == 3
        assert node.assigned_instance_id is None
        assert node.match_reason is None
        assert node.last_task_id is None
        assert node.dispatched_at is None


# ── MissionEvent (mission_id, seq) 唯一 + 软删复用 ──────────────────────────


async def _add_event(db, mission_id: str, org_id: str, seq: int) -> MissionEvent:
    ev = MissionEvent(
        mission_id=mission_id,
        org_id=org_id,
        seq=seq,
        event_type="mission_created",
        actor_type="user",
        content="test",
    )
    db.add(ev)
    return ev


async def test_mission_event_seq_unique():
    """同 (mission_id, seq) 二次插入违反 partial unique index。"""
    mission_id, m = await _make_mission()
    async with TestSessionLocal() as db:
        await _add_event(db, mission_id, m.org_id, seq=1)
        await db.commit()
        await _add_event(db, mission_id, m.org_id, seq=1)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_mission_event_seq_reusable_after_soft_delete():
    """软删行不参与唯一索引：soft_delete 后同 seq 可再插入（仓库 Partial Unique 约定）。"""
    mission_id, m = await _make_mission()
    async with TestSessionLocal() as db:
        first = await _add_event(db, mission_id, m.org_id, seq=1)
        await db.commit()
        first.soft_delete()
        await db.commit()
        await _add_event(db, mission_id, m.org_id, seq=1)
        await db.commit()  # 不抛 IntegrityError 即通过


# ── MissionEventCounter（seq 计数行）────────────────────────────────────────


async def test_mission_event_counter_round_trip():
    """计数行 PK=mission_id、next_seq 默认 0；直继 Base 无软删列。"""
    mission_id, _ = await _make_mission()
    async with TestSessionLocal() as db:
        counter = MissionEventCounter(mission_id=mission_id)
        db.add(counter)
        await db.commit()
        await db.refresh(counter)
        assert counter.mission_id == mission_id
        assert counter.next_seq == 0
        # 直继 Base（非 BaseModel）：基础设施表，无软删列
        assert not hasattr(MissionEventCounter, "deleted_at")


# ── CapabilityTag (org_id, tag) 唯一 ────────────────────────────────────────


async def test_capability_tag_unique_per_org():
    """非空 org scope 内 (org_id, tag) 唯一；不同 org 可同名。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org_a = Organization(name=f"tag-a-{suffix}", slug=f"tag-a-{suffix}")
        org_b = Organization(name=f"tag-b-{suffix}", slug=f"tag-b-{suffix}")
        db.add_all([org_a, org_b])
        await db.flush()
        db.add(CapabilityTag(org_id=org_a.id, tag="backend"))
        db.add(CapabilityTag(org_id=org_b.id, tag="backend"))
        await db.commit()
        db.add(CapabilityTag(org_id=org_a.id, tag="backend"))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# ── MissionOrgConfig 默认值 ─────────────────────────────────────────────────


async def test_mission_org_config_defaults():
    """每 org 一行：ttl 默认 14、token 保险丝默认不启用。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"cfg-org-{suffix}", slug=f"cfg-org-{suffix}")
        db.add(org)
        await db.flush()
        cfg = MissionOrgConfig(org_id=org.id)
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
        assert cfg.artifact_ttl_days == 14
        assert cfg.mission_token_fuse is None
        assert cfg.escalation_defaults is None


# ── MissionArtifact 默认值 ──────────────────────────────────────────────────


async def test_mission_artifact_defaults():
    """产物默认 retention=quarantine、version=1。"""
    mission_id, _ = await _make_mission()
    async with TestSessionLocal() as db:
        art = MissionArtifact(
            mission_id=mission_id,
            org_id=(await db.get(Mission, mission_id)).org_id,
            name="report.md",
            kind="report",
            storage_key=f"missions/x/y/{mission_id}/z/report.md",
        )
        db.add(art)
        await db.commit()
        await db.refresh(art)
        assert art.retention == "quarantine"
        assert art.version == 1
        assert art.expires_at is None
        assert art.promoted_by_user_id is None
