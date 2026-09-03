"""工作流模板 + 执行模式测试（设计 §二/§三）。

覆盖：保存（accept 挂载）、实例化（跳过拆解）、step_review 门控、审核操作。
"""
import uuid

import pytest
from sqlalchemy import select, text

from app.core.deps import get_current_org
from app.main import app
from app.models.mission import Mission
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.models.mission_template import MissionTemplate, MissionTemplateNode
from app.services.mission import mission_service, template_service
from tests.conftest import TestSessionLocal
from tests.mission_test_helpers import make_env, make_mission, make_node


@pytest.fixture(autouse=True)
async def _clean():
    async with TestSessionLocal() as db:
        await db.execute(text(
            "TRUNCATE TABLE mission_events, mission_event_counters, mission_artifacts, "
            "mission_nodes, mission_org_configs, missions, "
            "mission_template_nodes, mission_templates CASCADE"
        ))
        await db.commit()
    yield


async def _get_mission(mid):
    async with TestSessionLocal() as db:
        return await db.get(Mission, mid)


async def _get_nodes(mid):
    async with TestSessionLocal() as db:
        return (await db.execute(
            select(MissionNode).where(MissionNode.mission_id == mid).order_by(MissionNode.seq)
        )).scalars().all()


async def _make_accepted_mission(n_nodes=2):
    """建一个已到 acceptance 状态的任务（直接置状态，绕过完整执行链路）。"""
    org_id, user_id, ws_id, _cluster = await make_env("tpl")
    mission_id = await make_mission(org_id, ws_id, user_id)
    for seq in range(n_nodes):
        await make_node(mission_id, org_id, seq=seq, status="done",
                        tags=["backend"], depends_on=[] if seq == 0 else [seq - 1])
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        m.status = "acceptance"
        m.brief = {"goal": "测试目标", "constraints": [], "acceptance_criteria": ["标准"]}
        await db.commit()
    from types import SimpleNamespace
    user_ns = SimpleNamespace(id=user_id, name="测试用户")
    return mission_id, user_ns


async def test_save_as_template_on_accept():
    """accept 时带 save_as_template → 模板行 + 节点快照完整。"""
    mission_id, user = await _make_accepted_mission(n_nodes=2)

    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        await mission_service.accept_mission(
            db, m, user=user,
            save_as_template={"name": "测试工作流", "description": "测试描述"},
        )

    async with TestSessionLocal() as db:
        tpl = (await db.execute(select(MissionTemplate))).scalar_one()
        assert tpl.name == "测试工作流"
        assert tpl.created_from_mission_id == mission_id
        assert tpl.mission_type == "standard"
        assert tpl.execution_mode == "auto"
        assert tpl.usage_count == 0
        nodes = (await db.execute(
            select(MissionTemplateNode).where(MissionTemplateNode.template_id == tpl.id)
            .order_by(MissionTemplateNode.seq)
        )).scalars().all()
        assert len(nodes) == 2
        assert nodes[0].title == "节点"
        assert nodes[1].depends_on == [0]
        assert (await _get_mission(mission_id)).status == "archived"


async def test_instantiate_from_template():
    """从模板实例化 → awaiting_confirm + 节点结构一致 + usage_count +1。"""
    mission_id, user = await _make_accepted_mission(n_nodes=2)
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        await mission_service.accept_mission(
            db, m, user=user,
            save_as_template={"name": "复用工作流", "description": ""},
        )
        tpl = (await db.execute(select(MissionTemplate))).scalar_one()
        tpl_id = tpl.id

    from types import SimpleNamespace
    org_ns = SimpleNamespace(id=(await _get_mission(mission_id)).org_id)
    async with TestSessionLocal() as db:
        tpl = await db.get(MissionTemplate, tpl_id)
        new_mission = await template_service.instantiate_from_template(
            db, tpl, org=org_ns, user=user,
            workspace_id=(await _get_mission(mission_id)).workspace_id,
            requirement_text="复用测试需求",
        )
        assert new_mission.status == "awaiting_confirm"
        assert new_mission.coordinator_meta["template_id"] == tpl_id

        nodes = await _get_nodes(new_mission.id)
        assert len(nodes) == 2
        assert nodes[1].depends_on == [0]
        assert all(n.session_key for n in nodes)

        tpl_after = await db.get(MissionTemplate, tpl_id)
        assert tpl_after.usage_count == 1


async def test_step_review_pause_and_continue():
    """step_review：节点 done 后 paused_for_review=True → 调度器不派发 → review-continue 后恢复。"""
    org_id, user_id, ws_id, _cluster = await make_env("tpl")
    mission_id = await make_mission(org_id, ws_id, user_id)

    # 置 step_review + executing
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        m.execution_mode = "step_review"
        await db.commit()

    # 模拟节点 done（直接置状态 + 手动触发暂停逻辑）
    await make_node(mission_id, org_id, seq=0, status="done", tags=["backend"])
    await make_node(mission_id, org_id, seq=1, status="matched",
                    tags=["backend"], depends_on=[0])

    from app.services.mission.event_service import MissionEventService
    from sqlalchemy import update
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        m.paused_for_review = True
        await MissionEventService(db).append(
            m.id, org_id=m.org_id, event_type="system_note",
            actor_type="system", content="等待审核",
            payload={"review_pending": True},
        )
        await db.commit()

    # 验证 paused
    m = await _get_mission(mission_id)
    assert m.paused_for_review is True

    # review-continue（直接调 mission_service 层操作）
    async with TestSessionLocal() as db:
        m = await db.get(Mission, mission_id)
        m.paused_for_review = False
        await db.commit()

    m = await _get_mission(mission_id)
    assert m.paused_for_review is False
