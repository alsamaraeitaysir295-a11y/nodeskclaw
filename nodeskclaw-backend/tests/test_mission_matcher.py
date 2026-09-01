"""T4：匹配器 v1 + 覆盖检查（设计 §5）。

验收（设计 §11 T4）：标签命中 / 缺口 / 改派（花名册变化重匹配）三场景单测通过。
附带：显式空 capabilities 不回退基因名、manifest 损坏回退、并列取完成数多者。
纯 ORM/服务层，依赖 conftest 的 TestSessionLocal（真 PostgreSQL）。
"""
import json
import uuid

from app.models.cluster import Cluster
from app.models.gene import Gene, InstanceGene
from app.models.instance import Instance
from app.models.mission import Mission
from app.models.mission_node import MissionNode
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_agent import WorkspaceAgent
from app.services.mission.matcher import coverage_check, match_node
from tests.conftest import TestSessionLocal


async def _make_env() -> tuple[str, str]:
    """org/user/cluster/workspace；返回 (org_id, workspace_id)。

    后缀每次调用独立生成：conftest 的 drop_all 对循环 FK 静默失败（rbac/conftest
    因此改用 DROP SCHEMA），表在测试间不落清，唯一索引只能靠数据自带唯一后缀。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"mt-org-{suffix}", slug=f"mt-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"mt-user-{suffix}", email=f"mt-{suffix}@example.com")
        db.add(user)
        await db.flush()
        cluster = Cluster(name=f"mt-cluster-{suffix}", created_by=user.id)
        db.add(cluster)
        await db.flush()
        ws = Workspace(org_id=org.id, name=f"mt-ws-{suffix}", created_by=user.id)
        db.add(ws)
        await db.commit()
        return org.id, ws.id


async def _add_instance(org_id: str, ws_id: str, name: str) -> str:
    """空间里加一个 AI 员工实例（挂 WorkspaceAgent）；返回 instance_id。"""
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        creator = (await db.execute(select(User).limit(1))).scalar()
        inst = Instance(
            org_id=org_id,
            name=name,
            # slug 默认空串，同 org 多实例会撞 (slug, org_id) 唯一索引，必须自带唯一值
            slug=f"inst-{uuid.uuid4().hex[:8]}",
            cluster_id=(await db.execute(select(Cluster).limit(1))).scalar_one().id,
            namespace=f"ns-{uuid.uuid4().hex[:8]}",
            image_version="0.5.0",
            created_by=creator.id,
        )
        db.add(inst)
        await db.flush()
        db.add(WorkspaceAgent(workspace_id=ws_id, instance_id=inst.id, display_name=name))
        await db.commit()
        return inst.id


async def _install_gene(instance_id: str, name: str, manifest: dict | None) -> str:
    """给实例装一个基因（status=installed）；返回 gene_id。manifest=None 表示列缺失/损坏场景由调用方控制。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        gene = Gene(
            name=f"{name}-{suffix}",
            slug=f"{name}-{suffix}",
            # 服务层创建时才传播的血缘分组键，直建行需自带（无默认值）
            lineage_group_id=str(uuid.uuid4()),
            manifest=json.dumps(manifest, ensure_ascii=False) if manifest is not None else None,
        )
        db.add(gene)
        await db.flush()
        db.add(InstanceGene(instance_id=instance_id, gene_id=gene.id, status="installed"))
        await db.commit()
        return gene.id


async def _make_node(org_id: str, ws_id: str, tags: list[str]) -> MissionNode:
    """建 Mission + 单节点（capability_tags=tags）。"""
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        creator = (await db.execute(select(User).limit(1))).scalar()
        m = Mission(
            org_id=org_id, workspace_id=ws_id, title="匹配测试",
            requirement_text="匹配测试", created_by=creator.id,
        )
        db.add(m)
        await db.flush()
        node = MissionNode(
            mission_id=m.id, org_id=org_id, seq=0, title="测试节点",
            capability_tags=tags, session_key=f"{org_id}:{m.id}:n0",
        )
        db.add(node)
        await db.commit()
        await db.refresh(node)
        return node


# ── 场景一：标签命中 ────────────────────────────────────────────────────────


async def test_match_exact_tag_hit():
    """实例装了 capabilities=['backend'] 的基因，backend 节点精确命中（1.0 分）。"""
    org_id, ws_id = await _make_env()
    inst_id = await _add_instance(org_id, ws_id, "后端员工")
    await _install_gene(inst_id, "backend-pro-gene", {"capabilities": ["backend"]})
    node = await _make_node(org_id, ws_id, ["backend"])

    async with TestSessionLocal() as db:
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        assert result.chosen_instance_id == inst_id
        assert result.candidates[0].score == 1.0
        assert result.capability_gap == []


async def test_match_partial_and_similar_scores():
    """包含关系 0.6 / 相似度 0.4 打分正确（webdevelopment ⊃ development）。"""
    org_id, ws_id = await _make_env()
    inst_a = await _add_instance(org_id, ws_id, "全栈员工")
    await _install_gene(inst_a, "fullstack-gene", {"capabilities": ["webdevelopment"]})

    async with TestSessionLocal() as db:
        node = await _make_node(org_id, ws_id, ["development"])
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        # development ⊂ webdevelopment → 0.6
        assert result.candidates[0].score == 0.6


# ── 场景二：缺口 ────────────────────────────────────────────────────────────


async def test_match_capability_gap():
    """无任何实例可命中 payment 标签 → 无 chosen，gap=['payment']。"""
    org_id, ws_id = await _make_env()
    inst_id = await _add_instance(org_id, ws_id, "文案员工")
    await _install_gene(inst_id, "writer-gene", {"capabilities": ["copywriting"]})
    node = await _make_node(org_id, ws_id, ["payment"])

    async with TestSessionLocal() as db:
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        assert result.chosen_instance_id is None
        assert result.capability_gap == ["payment"]


# ── 场景三：花名册变化重匹配（改派基础）────────────────────────────────────


async def test_rematch_after_roster_change():
    """新进更匹配的员工后重跑匹配，chosen 从旧实例切到新实例。"""
    org_id, ws_id = await _make_env()
    inst_old = await _add_instance(org_id, ws_id, "通用员工")
    await _install_gene(inst_old, "misc-gene", {"capabilities": ["fullstack"]})
    node = await _make_node(org_id, ws_id, ["backend"])

    async with TestSessionLocal() as db:
        node = await db.get(MissionNode, node.id)
        before = await match_node(db, node)
        assert before.chosen_instance_id is None  # 全栈基因对 backend 无命中

    inst_new = await _add_instance(org_id, ws_id, "后端专家")
    await _install_gene(inst_new, "backend-gene", {"capabilities": ["backend"]})

    async with TestSessionLocal() as db:
        node = await db.get(MissionNode, node.id)
        after = await match_node(db, node)
        assert after.chosen_instance_id == inst_new
        assert after.candidates[0].score == 1.0


# ── 标注语义与回退 ─────────────────────────────────────────────────────────


async def test_empty_capabilities_no_name_fallback():
    """capabilities 显式空数组（行为型基因）不回退基因名，不产生命中。"""
    org_id, ws_id = await _make_env()
    inst_id = await _add_instance(org_id, ws_id, "文化员工")
    # 基因名叫 backend-culture 诱惑回退，但 capabilities=[] 必须不参与匹配
    await _install_gene(inst_id, "backend-culture", {"capabilities": []})

    async with TestSessionLocal() as db:
        node = await _make_node(org_id, ws_id, ["backend"])
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        assert result.chosen_instance_id is None


async def test_manifest_missing_falls_back_to_gene_name():
    """manifest 无 capabilities 键（或损坏）回退基因名做匹配（含后缀名走包含关系 0.6）。"""
    org_id, ws_id = await _make_env()
    inst_id = await _add_instance(org_id, ws_id, "老基因员工")
    await _install_gene(inst_id, "backend", None)  # manifest=NULL → 回退基因名

    async with TestSessionLocal() as db:
        node = await _make_node(org_id, ws_id, ["backend"])
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        assert result.chosen_instance_id == inst_id
        # 基因名带运行后缀（backend-xxxx），tag "backend" 与之构成包含关系 → 0.6；
        # 若未回退（manifest 无 capabilities 键不取基因名）则为 0 且无 chosen
        assert result.candidates[0].score == 0.6


# ── 并列取历史完成数多者 ───────────────────────────────────────────────────


async def test_tie_break_by_done_count():
    """同分并列时选历史完成节点数多的实例。"""
    org_id, ws_id = await _make_env()
    inst_a = await _add_instance(org_id, ws_id, "员工A")
    inst_b = await _add_instance(org_id, ws_id, "员工B")
    await _install_gene(inst_a, "a-gene", {"capabilities": ["backend"]})
    await _install_gene(inst_b, "b-gene", {"capabilities": ["backend"]})

    async with TestSessionLocal() as db:
        from sqlalchemy import select
        creator = (await db.execute(select(User).limit(1))).scalar()
        m = Mission(
            org_id=org_id, workspace_id=ws_id, title="历史任务",
            requirement_text="历史", created_by=creator.id,
        )
        db.add(m)
        await db.flush()
        done_node = MissionNode(
            mission_id=m.id, org_id=org_id, seq=0, title="已完成节点",
            capability_tags=["backend"], session_key=f"{org_id}:{m.id}:n0",
            status="done", assigned_instance_id=inst_b,
        )
        db.add(done_node)
        await db.commit()

        node = await _make_node(org_id, ws_id, ["backend"])
        node = await db.get(MissionNode, node.id)
        result = await match_node(db, node)
        # 两实例同 1.0 分，B 有 1 个 done 历史 → chosen=B
        assert result.chosen_instance_id == inst_b


# ── 覆盖检查（dry-run）─────────────────────────────────────────────────────


async def test_coverage_check_returns_all_nodes():
    """覆盖检查对全部节点 dry-run，输出建议人选与缺口。"""
    org_id, ws_id = await _make_env()
    inst_id = await _add_instance(org_id, ws_id, "后端员工")
    await _install_gene(inst_id, "backend-gene", {"capabilities": ["backend"]})

    async with TestSessionLocal() as db:
        from sqlalchemy import select
        creator = (await db.execute(select(User).limit(1))).scalar()
        m = Mission(
            org_id=org_id, workspace_id=ws_id, title="覆盖检查",
            requirement_text="覆盖", created_by=creator.id,
        )
        db.add(m)
        await db.flush()
        for seq, tags in enumerate([["backend"], ["payment"]]):
            db.add(MissionNode(
                mission_id=m.id, org_id=org_id, seq=seq, title=f"节点{seq}",
                capability_tags=tags, session_key=f"{org_id}:{m.id}:n{seq}",
            ))
        await db.commit()
        mission_id = m.id

    async with TestSessionLocal() as db:
        rows = await coverage_check(db, mission_id)
        assert len(rows) == 2
        assert rows[0]["suggested_instance_id"] == inst_id
        assert rows[0]["capability_gap"] == []
        assert rows[1]["suggested_instance_id"] is None
        assert rows[1]["capability_gap"] == ["payment"]
