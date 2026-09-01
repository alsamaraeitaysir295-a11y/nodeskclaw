"""Mission 系列测试共享 fixture 助手（真 PostgreSQL，配合 conftest.TestSessionLocal）。

注意：conftest 的 drop_all 对循环 FK 静默失败（rbac/conftest 因此用 DROP SCHEMA），
表在测试间不落清——所有带唯一约束的数据（org slug / instance slug / gene 名）必须
自带每次调用独立的 uuid 后缀。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.cluster import Cluster
from app.models.gene import Gene, InstanceGene
from app.models.instance import Instance
from app.models.mission import Mission
from app.models.mission_node import MissionNode
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_agent import WorkspaceAgent
from tests.conftest import TestSessionLocal


def utc_past(seconds: int) -> datetime:
    """N 秒前的 aware UTC 时间（写 dispatched_at 等簿记字段模拟超时用）。"""
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def make_env(prefix: str = "ms") -> tuple[str, str, str, str]:
    """建 org/user/cluster/workspace；返回 (org_id, user_id, workspace_id, cluster_id)。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"{prefix}-org-{suffix}", slug=f"{prefix}-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"{prefix}-user-{suffix}", email=f"{prefix}-{suffix}@example.com")
        db.add(user)
        await db.flush()
        cluster = Cluster(name=f"{prefix}-cluster-{suffix}", created_by=user.id)
        db.add(cluster)
        await db.flush()
        ws = Workspace(org_id=org.id, name=f"{prefix}-ws-{suffix}", created_by=user.id)
        db.add(ws)
        await db.commit()
        return org.id, user.id, ws.id, cluster.id


async def add_instance(org_id: str, ws_id: str, cluster_id: str, name: str) -> str:
    """空间里加 AI 员工实例（挂 WorkspaceAgent）；返回 instance_id。"""
    async with TestSessionLocal() as db:
        creator = (await db.execute(select(User).limit(1))).scalar()
        inst = Instance(
            org_id=org_id,
            name=name,
            slug=f"inst-{uuid.uuid4().hex[:8]}",  # slug 默认空串，同 org 多实例会撞唯一索引
            cluster_id=cluster_id,
            namespace=f"ns-{uuid.uuid4().hex[:8]}",
            image_version="0.5.0",
            created_by=creator.id,
        )
        db.add(inst)
        await db.flush()
        db.add(WorkspaceAgent(workspace_id=ws_id, instance_id=inst.id, display_name=name))
        await db.commit()
        return inst.id


async def install_gene(instance_id: str, name: str, manifest: dict | None) -> str:
    """给实例装基因（status=installed）；manifest=None 模拟列缺失/损坏。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        gene = Gene(
            name=f"{name}-{suffix}",
            slug=f"{name}-{suffix}",
            lineage_group_id=str(uuid.uuid4()),  # 服务层创建时才传播，直建需自带（无默认）
            manifest=json.dumps(manifest, ensure_ascii=False) if manifest is not None else None,
        )
        db.add(gene)
        await db.flush()
        db.add(InstanceGene(instance_id=instance_id, gene_id=gene.id, status="installed"))
        await db.commit()
        return gene.id


async def make_mission(
    org_id: str, ws_id: str, user_id: str, *,
    title: str = "测试任务", status: str = "executing",
    token_cost: int = 0, fuse_acknowledged: bool = False,
) -> str:
    async with TestSessionLocal() as db:
        m = Mission(
            org_id=org_id, workspace_id=ws_id, title=title,
            requirement_text=title, created_by=user_id, status=status,
            token_cost=token_cost,
            fuse_acknowledged_at=datetime.now(timezone.utc) if fuse_acknowledged else None,
        )
        db.add(m)
        await db.commit()
        return m.id


async def make_node(
    mission_id: str, org_id: str, *, seq: int = 0, title: str = "节点",
    tags: list[str] | None = None, depends_on: list[int] | None = None,
    status: str = "pending", assigned_instance_id: str | None = None,
    attempt_count: int = 0, dispatched_at: datetime | None = None,
    started_at: datetime | None = None,
) -> str:
    async with TestSessionLocal() as db:
        node = MissionNode(
            mission_id=mission_id, org_id=org_id, seq=seq, title=title,
            capability_tags=tags or [], depends_on=depends_on or [],
            status=status, assigned_instance_id=assigned_instance_id,
            session_key=f"{org_id}:{mission_id}:n{seq}",
            attempt_count=attempt_count,
            dispatched_at=dispatched_at, started_at=started_at,
        )
        db.add(node)
        await db.commit()
        return node.id
