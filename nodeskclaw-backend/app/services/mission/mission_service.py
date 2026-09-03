"""任务空间业务服务（设计 §9 / T6）。

职责：Mission 生命周期编排（创建/拆解/确认/改派/验收/取消）+ 事件落档。
拆解为后台任务（asyncio.create_task，重启丢失由 draft 态重试兜底——同 deploy
管道的已知坑，见 main.py 注释）；LLM 调用经 chat 参数注入便于测试。
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, NotFoundError
from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent, MissionEventCounter
from app.models.mission_node import MissionNode
from app.services.mission.event_service import MissionEventService
from app.services.mission.matcher import coverage_check
from app.services.mission.orchestrator import (
    DecompositionError,
    decompose,
    load_active_capability_tags,
    org_default_chat,
)

logger = logging.getLogger(__name__)

# 可回 draft 重试拆解的前态（replan 从 awaiting_confirm 回拆）
_DECOMPOSABLE_FROM = ("draft", "awaiting_confirm")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def broadcast_mission_event(workspace_id: str, event_type: str, data: dict) -> None:
    """把任务事件推到协作空间聊天流（Phase 2 聊天式任务流）。"""
    try:
        from app.api.workspaces import broadcast_event
        broadcast_event(workspace_id, event_type, data)
    except Exception:
        pass  # 广播失败不影响任务主流程


async def get_mission(db: AsyncSession, mission_id: str) -> Mission:
    m = await db.get(Mission, mission_id)
    if m is None or m.deleted_at is not None:
        raise NotFoundError("任务不存在", "errors.mission.not_found")
    return m


async def get_mission_nodes(db: AsyncSession, mission_id: str) -> list[MissionNode]:
    return (await db.execute(
        select(MissionNode)
        .where(MissionNode.mission_id == mission_id, not_deleted(MissionNode))
        .order_by(MissionNode.seq)
    )).scalars().all()


async def create_mission(
    db: AsyncSession, *, org, user, workspace_id: str, requirement_text: str,
) -> Mission:
    """创建 draft 任务 + 事件计数行；拆解由调用方在 commit 后调度后台任务。"""
    mission = Mission(
        org_id=org.id,
        workspace_id=workspace_id,
        title=requirement_text[:60] or "新任务",
        requirement_text=requirement_text,
        created_by=user.id,
        status="draft",
    )
    db.add(mission)
    await db.flush()
    db.add(MissionEventCounter(mission_id=mission.id))
    await MissionEventService(db).append(
        mission.id, org_id=org.id,
        event_type="mission_created", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"任务已创建：{mission.title}",
    )
    await db.commit()
    return mission


async def run_decomposition(mission_id: str, *, chat=None, session_factory=None) -> bool:
    """后台拆解（自带会话）。成功 → awaiting_confirm（轻任务直接 executing）；
    失败 → decomposition_failed + 回原状态（draft/awaiting_confirm）允许重试。
    chat / session_factory 注入点供测试替换（默认生产引擎）。
    """
    if session_factory is None:
        from app.core.deps import async_session_factory as session_factory

    async with session_factory() as db:
        mission = await get_mission(db, mission_id)
        from_status = mission.status
        res = await db.execute(
            update(Mission)
            .where(Mission.id == mission_id, Mission.status.in_(_DECOMPOSABLE_FROM))
            .values(status="decomposing")
        )
        if not res.rowcount:
            return False
        svc = MissionEventService(db)
        await svc.append(
            mission_id, org_id=mission.org_id,
            event_type="decomposition_started", actor_type="system",
            content="开始拆解需求",
        )
        try:
            tags = await load_active_capability_tags(db, mission.org_id)
            chat_fn = chat or (lambda prompt: org_default_chat(db, mission.org_id, prompt))
            result = await decompose(mission.requirement_text, tags, chat=chat_fn)
        except DecompositionError as e:
            await svc.append(
                mission_id, org_id=mission.org_id,
                event_type="decomposition_failed", actor_type="system",
                content=f"拆解失败：{e}",
            )
            await db.execute(
                update(Mission)
                .where(Mission.id == mission_id, Mission.status == "decomposing")
                .values(status=from_status)
            )
            await db.commit()
            return False

        mission.title = result.mission_title
        mission.brief = result.brief
        mission.coordinator_meta = result.coordinator_meta
        mission.escalation_policy = {"l2_rules": result.l2_rules, "l1_hint": result.l1_hint}
        nodes_payload = []
        for i, n in enumerate(result.nodes):
            node_id = str(uuid.uuid4())
            node = MissionNode(
                id=node_id,  # session_key 含 node.id，须在 INSERT 前显式生成（非空约束）
                mission_id=mission_id, org_id=mission.org_id, seq=i,
                title=n.title, description=n.description,
                acceptance_criteria=n.acceptance_criteria,
                capability_tags=n.capability_tags, depends_on=n.depends_on,
                session_key=f"{mission.org_id}:{mission_id}:{node_id}",
            )
            db.add(node)
            nodes_payload.append({
                "id": node.id, "seq": node.seq, "title": node.title,
                "capability_tags": node.capability_tags, "depends_on": node.depends_on,
            })

        mission_type = result.mission_type
        mission.mission_type = mission_type
        next_status = "executing" if mission_type == "lightweight" else "awaiting_confirm"
        await svc.append(
            mission_id, org_id=mission.org_id,
            event_type="decomposition_done", actor_type="system",
            content=f"拆解完成：{len(result.nodes)} 个节点"
                    + ("（轻任务，直接执行）" if mission_type == "lightweight" else "，请确认任务图"),
            payload={"nodes": nodes_payload, "mission_type": mission_type},
        )
        await db.execute(
            update(Mission)
            .where(Mission.id == mission_id, Mission.status == "decomposing")
            .values(status=next_status)
        )
        await db.commit()

        # Phase 2：推到协作空间聊天流
        broadcast_mission_event(mission.workspace_id, "mission:decomposed", {
            "mission_id": mission_id,
            "title": result.mission_title,
            "node_count": len(result.nodes),
            "mission_type": mission_type,
            "status": next_status,
            "nodes": [
                {"seq": n["seq"], "title": n["title"], "tags": n["capability_tags"]}
                for n in nodes_payload
            ],
        })
        return True


async def confirm_mission(
    db: AsyncSession, mission: Mission, *, user,
    node_edits: list[dict] | None = None,
    add_nodes: list[dict] | None = None,
    remove_node_ids: list[str] | None = None,
) -> None:
    """确认任务图（可携带人工编辑）→ executing。仅 awaiting_confirm 可确认。"""
    if mission.status != "awaiting_confirm":
        raise BadRequestError("任务当前状态不可确认", "errors.mission.invalid_state")
    svc = MissionEventService(db)
    edited = False

    nodes = await get_mission_nodes(db, mission.id)
    by_id = {n.id: n for n in nodes}
    by_seq = {n.seq: n for n in nodes}

    for edit in node_edits or []:
        node = by_id.get(edit.get("node_id") or "")
        if node is None or node.status != "pending":
            continue
        for field in ("title", "description", "acceptance_criteria", "capability_tags"):
            if field in edit and edit[field] is not None:
                setattr(node, field, edit[field])
        if edit.get("assigned_instance_id"):
            node.assigned_instance_id = edit["assigned_instance_id"]
        edited = True

    if remove_node_ids:
        removable = set(remove_node_ids)
        # 被依赖的节点不可删（下游依赖会永久悬空）
        depended = {d for n in nodes for d in (n.depends_on or [])}
        for nid in removable:
            node = by_id.get(nid)
            if node is None or node.status != "pending":
                continue
            if node.seq in depended:
                raise BadRequestError(
                    f"节点「{node.title}」被下游依赖，不可删除", "errors.mission.node_depended",
                )
            node.soft_delete()
            edited = True

    if add_nodes:
        next_seq = max((n.seq for n in nodes), default=-1) + 1
        for spec in add_nodes:
            node_id = str(uuid.uuid4())
            db.add(MissionNode(
                id=node_id,
                mission_id=mission.id, org_id=mission.org_id, seq=next_seq,
                title=spec.get("title", "新节点"),
                description=spec.get("description", ""),
                acceptance_criteria=spec.get("acceptance_criteria", ""),
                capability_tags=spec.get("capability_tags", []),
                depends_on=spec.get("depends_on", []),
                assigned_instance_id=spec.get("assigned_instance_id"),
                session_key=f"{mission.org_id}:{mission.id}:{node_id}",
            ))
            next_seq += 1
            edited = True

    await svc.append(
        mission.id, org_id=mission.org_id,
        event_type="dag_edited" if edited else "dag_confirmed",
        actor_type="user", actor_id=user.id, actor_name=getattr(user, "name", None),
        content="任务图已确认，开始执行",
    )
    await db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status == "awaiting_confirm")
        .values(status="executing")
    )
    await db.commit()


async def replan_mission(db: AsyncSession, mission: Mission) -> None:
    """重拆：冻结 done/running/dispatched/acked，软删其余节点后回拆解。"""
    svc = MissionEventService(db)
    nodes = await get_mission_nodes(db, mission.id)
    frozen = {"done", "running", "dispatched", "acked"}
    for node in nodes:
        if node.status not in frozen:
            node.soft_delete()
    await svc.append(
        mission.id, org_id=mission.org_id,
        event_type="system_note", actor_type="system",
        content="重新拆解未开工节点（已完成/进行中节点保持不变）",
    )
    await db.commit()
    spawn_decomposition(mission.id)


def _log_task_exception(task: asyncio.Task) -> None:
    """检索并记录后台任务异常——未检索的异常会在事件循环/测试层报错。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("后台拆解任务失败: %s", exc, exc_info=exc)


def spawn_decomposition(mission_id: str, *, chat=None, session_factory=None) -> asyncio.Task:
    """以后台任务运行拆解（API/replan 用），异常只记录不上抛。"""
    task = asyncio.create_task(run_decomposition(
        mission_id, chat=chat, session_factory=session_factory,
    ))
    task.add_done_callback(_log_task_exception)
    return task


async def set_mission_priority(
    db: AsyncSession, mission: Mission, priority: int, *, user,
) -> None:
    """P2 优先级插队：0=normal / 1=urgent。仅发起人或 org admin 可改。"""
    if mission.created_by != user.id and not getattr(user, "is_super_admin", False):
        from app.models.org_membership import OrgMembership
        role = (await db.execute(
            select(OrgMembership.role).where(
                OrgMembership.org_id == mission.org_id,
                OrgMembership.user_id == user.id,
                not_deleted(OrgMembership),
            )
        )).scalar_one_or_none()
        if role != "admin":
            from app.core.exceptions import ForbiddenError
            raise ForbiddenError("仅发起人或管理员可设置优先级", "errors.mission.not_acceptor")

    if mission.priority == priority:
        return
    mission.priority = priority
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id,
        event_type="system_note", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"任务优先级已设为{'紧急' if priority == 1 else '普通'}",
    )
    await db.commit()


async def cancel_mission(db: AsyncSession, mission: Mission, *, user) -> None:
    """任意时刻取消；未完成节点置 skipped。"""
    svc = MissionEventService(db)
    res = await db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status.in_(("executing", "blocked_question", "acceptance")))
        .values(status="cancelled")
    )
    if not res.rowcount:
        raise BadRequestError("任务已结束，无法取消", "errors.mission.invalid_state")
    unfinished = ("pending", "matched", "dispatched", "acked", "running", "blocked_question", "blocked_dependency")
    await db.execute(
        update(MissionNode)
        .where(MissionNode.mission_id == mission.id, MissionNode.status.in_(unfinished), not_deleted(MissionNode))
        .values(status="skipped")
    )
    await svc.append(
        mission.id, org_id=mission.org_id,
        event_type="mission_cancelled", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content="任务已取消",
    )
    await db.commit()


async def delete_mission(db: AsyncSession, mission: Mission, *, user) -> None:
    """删除对话：任务全链软删（mission/nodes/events/artifacts 一并不可见）。

    任意状态可删；活跃态先 CAS 置 cancelled（调度器/拆解后台任务不再处理），
    未完成节点置 skipped（与 cancel 同语义）。软删后插件迟到回报在
    ingest 入口被 not_deleted(MissionNode) 拒绝，无需额外清理。
    """
    active = ("draft", "decomposing", "awaiting_confirm", "executing",
              "blocked_question", "acceptance")
    await db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status.in_(active))
        .values(status="cancelled", paused_for_review=False)
    )
    unfinished = ("pending", "matched", "dispatched", "acked", "running",
                  "blocked_question", "blocked_dependency")
    await db.execute(
        update(MissionNode)
        .where(MissionNode.mission_id == mission.id, MissionNode.status.in_(unfinished), not_deleted(MissionNode))
        .values(status="skipped")
    )
    now = _utcnow()
    mission.soft_delete()
    for table in (MissionNode, MissionEvent, MissionArtifact):
        await db.execute(
            update(table)
            .where(table.mission_id == mission.id, not_deleted(table))
            .values(deleted_at=now)
        )
    await db.commit()
    broadcast_mission_event(mission.workspace_id, "mission:deleted", {
        "mission_id": mission.id, "title": mission.title,
    })


async def retry_node(db: AsyncSession, mission: Mission, node: MissionNode) -> None:
    """失败节点重试：回 pending、attempt 清零。"""
    if node.status != "failed":
        raise BadRequestError("仅失败节点可重试", "errors.mission.invalid_state")
    node.status = "pending"
    node.attempt_count = 0
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id, node_id=node.id,
        event_type="system_note", actor_type="user",
        content=f"节点「{node.title}」已重置重试",
    )
    await db.commit()


async def reassign_node(
    db: AsyncSession, mission: Mission, node: MissionNode, instance_id: str, *, user,
) -> None:
    """人工改派：换实例并回 matched（打回验收失败语义，不混入 failed）。"""
    if node.status not in ("pending", "matched", "failed"):
        raise BadRequestError("节点当前状态不可改派", "errors.mission.invalid_state")
    node.status = "matched"
    node.assigned_instance_id = instance_id
    node.attempt_count = 0
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id, node_id=node.id,
        event_type="system_note", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"节点「{node.title}」已改派",
        payload={"assigned_instance_id": instance_id},
    )
    await db.commit()


async def answer_question(
    db: AsyncSession, mission: Mission, event: MissionEvent, answer: str, *, user,
) -> None:
    """回答 L1/L2/保险丝提问；解除阻塞并落 question_answered 事件。"""
    if event.event_type not in ("l1_question", "l2_question", "token_fuse_tripped"):
        raise BadRequestError("该事件不是待回答的提问", "errors.mission.not_a_question")
    svc = MissionEventService(db)

    if event.event_type == "token_fuse_tripped":
        # P2 硬阻断：确认后阈值翻倍（fuse * 2^(count+1)），继续监控不豁免
        await db.execute(
            update(Mission)
            .where(Mission.id == mission.id, Mission.status == "blocked_question")
            .values(
                status="executing",
                fuse_acknowledged_at=_utcnow(),
                fuse_ack_count=Mission.fuse_ack_count + 1,
            )
        )
    else:
        await db.execute(
            update(Mission)
            .where(Mission.id == mission.id, Mission.status == "blocked_question")
            .values(status="executing")
        )
        if event.node_id:
            await db.execute(
                update(MissionNode)
                .where(MissionNode.id == event.node_id, MissionNode.status == "blocked_question")
                .values(status="running")
            )

    await svc.append(
        mission.id, org_id=mission.org_id, node_id=event.node_id,
        event_type="question_answered", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=answer,
        payload={"answers_event_seq": event.seq},
    )

    # 编排者复核升级 L2 的人工答复：含 force_accept 关键词 → 强制验收通过该节点
    ep = event.payload if isinstance(event.payload, dict) else {}
    force_kw = ep.get("force_accept")
    if event.node_id and isinstance(force_kw, list) and any(k in answer for k in force_kw):
        node = await db.get(MissionNode, event.node_id)
        if node is not None and node.mission_id == mission.id and node.status not in ("done", "skipped"):
            await db.execute(
                update(MissionNode)
                .where(MissionNode.id == node.id)
                .values(status="done", finished_at=_utcnow())
            )
            await svc.append(
                mission.id, org_id=mission.org_id, node_id=node.id,
                event_type="node_done", actor_type="user",
                actor_id=user.id, actor_name=getattr(user, "name", None),
                content=f"人工验收通过：{answer}",
            )
    await db.commit()


async def accept_mission(
    db: AsyncSession, mission: Mission, *, user,
    save_as_template: dict | None = None,
) -> None:
    """验收通过：acceptance → archived。仅发起人或 org admin。

    save_as_template={name, description} 非空时同事务保存工作流模板（原子绑定）。
    """
    res = await db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status == "acceptance")
        .values(status="archived")
    )
    if not res.rowcount:
        raise BadRequestError("任务不在待验收状态", "errors.mission.invalid_state")
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id,
        event_type="mission_accepted", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content="验收通过，任务已归档",
    )
    if save_as_template and save_as_template.get("name"):
        from app.services.mission.template_service import save_as_template as _save
        template = await _save(
            db, mission,
            name=save_as_template["name"],
            description=save_as_template.get("description") or "",
            user=user,
        )
        await MissionEventService(db).append(
            mission.id, org_id=mission.org_id,
            event_type="system_note", actor_type="user",
            content=f"已保存为工作流模板「{template.name}」",
        )
    await db.commit()


async def reject_mission(
    db: AsyncSession, mission: Mission, *, reason: str, rejected_nodes: list[str], user,
) -> None:
    """节点级打回：点名节点回 pending（attempt 重置），Mission 回 executing。"""
    svc = MissionEventService(db)
    res = await db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status == "acceptance")
        .values(status="executing")
    )
    if not res.rowcount:
        raise BadRequestError("任务不在待验收状态", "errors.mission.invalid_state")
    rejected_titles: list[str] = []
    if rejected_nodes:
        nodes = await db.execute(
            select(MissionNode).where(
                MissionNode.mission_id == mission.id,
                MissionNode.id.in_(rejected_nodes),
                MissionNode.status == "done",
                not_deleted(MissionNode),
            )
        )
        for node in nodes.scalars().all():
            node.status = "pending"
            node.attempt_count = 0
            rejected_titles.append(node.title)
    await svc.append(
        mission.id, org_id=mission.org_id,
        event_type="mission_rejected", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"验收打回（{', '.join(rejected_titles) or '全部'}）：" + reason,
        payload={"reason": reason, "rejected_nodes": rejected_nodes},
    )
    await db.commit()


async def promote_artifact(db: AsyncSession, mission: Mission, artifact: MissionArtifact, *, user) -> None:
    """隔离区产物转正。"""
    if artifact.retention != "quarantine":
        raise BadRequestError("产物不在隔离区", "errors.mission.invalid_state")
    artifact.retention = "promoted"
    artifact.expires_at = None
    artifact.promoted_by_user_id = user.id
    artifact.promoted_at = _utcnow()
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id, node_id=artifact.node_id,
        event_type="artifact_promoted", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"产物「{artifact.name}」已保留转正",
    )
    await db.commit()


async def get_events(
    db: AsyncSession, mission_id: str, *, after_seq: int = 0, kinds: list[str] | None = None,
) -> list[MissionEvent]:
    stmt = select(MissionEvent).where(
        MissionEvent.mission_id == mission_id,
        MissionEvent.seq > after_seq,
        not_deleted(MissionEvent),
    )
    if kinds:
        stmt = stmt.where(MissionEvent.event_type.in_(kinds))
    return (await db.execute(stmt.order_by(MissionEvent.seq))).scalars().all()


async def mission_detail(db: AsyncSession, mission: Mission) -> dict[str, Any]:
    """详情序列化：awaiting_confirm 附带覆盖检查（建议人选/缺口）。"""
    nodes = await get_mission_nodes(db, mission.id)
    data = {
        "id": mission.id,
        "workspace_id": mission.workspace_id,
        "title": mission.title,
        "requirement_text": mission.requirement_text,
        "brief": mission.brief,
        "mission_type": mission.mission_type,
        "status": mission.status,
        "created_by": mission.created_by,
        "artifact_ttl_days": mission.artifact_ttl_days,
        "tokens": {
            "cost": mission.token_cost,
            "prompt": mission.prompt_token_cost,
            "completion": mission.completion_token_cost,
        },
        "coordinator_meta": mission.coordinator_meta,
        "nodes": [
            {
                "id": n.id, "seq": n.seq, "title": n.title,
                "description": n.description,
                "acceptance_criteria": n.acceptance_criteria,
                "capability_tags": n.capability_tags, "depends_on": n.depends_on,
                "status": n.status,
                "assigned_instance_id": n.assigned_instance_id,
                "match_reason": n.match_reason,
                "attempt_count": n.attempt_count,
                "session_key": n.session_key,
            }
            for n in nodes
        ],
    }
    if mission.status == "awaiting_confirm":
        data["coverage"] = await coverage_check(db, mission.id)
    return data
