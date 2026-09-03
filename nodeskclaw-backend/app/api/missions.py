"""任务空间 API（设计 §9 / T6）。

鉴权：get_current_org（org 上下文）+ workspace 成员检查（复用 wm_service）；
验收/打回限发起人或 org admin。
SSE：P1 采用 2s 轮询增量（after_seq），pg_notify 推送接入留待后续（行为等价）。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_org, get_db
from app.core.exceptions import NotFoundError
from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.models.org_membership import OrgMembership
from app.schemas.common import ApiResponse
from app.services import workspace_member_service as wm_service
from app.services.mission import mission_service

logger = logging.getLogger(__name__)

router = APIRouter()


class MissionCreateRequest(BaseModel):
    requirement_text: str
    mission_type: str | None = None  # 预留：P1 由编排器自动判定


class NodeEditRequest(BaseModel):
    node_id: str
    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    capability_tags: list[str] | None = None
    assigned_instance_id: str | None = None


class NodeAddRequest(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    capability_tags: list[str] = []
    depends_on: list[int] = []
    assigned_instance_id: str | None = None


class MissionConfirmRequest(BaseModel):
    node_edits: list[NodeEditRequest] | None = None
    add_nodes: list[NodeAddRequest] | None = None
    remove_node_ids: list[str] | None = None


class MissionRejectRequest(BaseModel):
    reason: str = ""
    rejected_nodes: list[str] = []


class QuestionAnswerRequest(BaseModel):
    answer: str


class SaveAsTemplateRequest(BaseModel):
    name: str
    description: str = ""


class TemplateRunRequest(BaseModel):
    requirement_text: str
    execution_mode: str | None = None  # 覆盖模板的 auto / step_review


class ReviewContinueRequest(BaseModel):
    action: str  # approve / reject / pause
    node_id: str | None = None  # reject 时指定打回的节点


class PriorityRequest(BaseModel):
    level: str  # "normal" | "urgent"


class ReassignRequest(BaseModel):
    instance_id: str


async def _load_mission(db: AsyncSession, mission_id: str, user):
    """取任务 + 成员校验（跨 org 不可见：成员检查要求同 org 成员）。"""
    mission = await mission_service.get_mission(db, mission_id)
    await wm_service.check_workspace_member(mission.workspace_id, user, db)
    return mission


def _query_user_dep():
    """EventSource 无法带 Authorization 头，SSE 走 query token（同 workspaces）。"""
    from app.core.security import get_current_user_from_query
    return get_current_user_from_query


async def _require_accept_right(db: AsyncSession, mission, user) -> None:
    """验收权：发起人或 org admin。"""
    if mission.created_by == user.id:
        return
    role = (await db.execute(
        select(OrgMembership.role).where(
            OrgMembership.org_id == mission.org_id,
            OrgMembership.user_id == user.id,
            not_deleted(OrgMembership),
        )
    )).scalar_one_or_none()
    if role != "admin":
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("仅发起人或组织管理员可验收", "errors.mission.not_acceptor")


@router.post("/workspaces/{workspace_id}/missions", response_model=ApiResponse)
async def create_mission(
    workspace_id: str,
    body: MissionCreateRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, org = ctx
    await wm_service.check_workspace_member(workspace_id, user, db)
    mission = await mission_service.create_mission(
        db, org=org, user=user,
        workspace_id=workspace_id, requirement_text=body.requirement_text,
    )
    mission_service.spawn_decomposition(mission.id)
    return ApiResponse(data={"id": mission.id, "status": mission.status})


@router.get("/workspaces/{workspace_id}/missions", response_model=ApiResponse)
async def list_missions(
    workspace_id: str,
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    from app.models.mission import Mission
    user, _org = ctx
    await wm_service.check_workspace_member(workspace_id, user, db)
    stmt = select(Mission).where(
        Mission.workspace_id == workspace_id, not_deleted(Mission),
    )
    if status:
        stmt = stmt.where(Mission.status == status)
    missions = (await db.execute(stmt.order_by(Mission.created_at.desc()))).scalars().all()
    return ApiResponse(data=[{
        "id": m.id, "title": m.title, "status": m.status,
        "mission_type": m.mission_type,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "tokens": m.token_cost + m.prompt_token_cost + m.completion_token_cost,
    } for m in missions])


@router.get("/missions/{mission_id}", response_model=ApiResponse)
async def get_mission_detail(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    return ApiResponse(data=await mission_service.mission_detail(db, mission))


@router.post("/missions/{mission_id}/confirm", response_model=ApiResponse)
async def confirm_mission(
    mission_id: str,
    body: MissionConfirmRequest | None = None,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    body = body or MissionConfirmRequest()
    await mission_service.confirm_mission(
        db, mission,
        user=user,
        node_edits=[e.model_dump() for e in body.node_edits] if body.node_edits else None,
        add_nodes=[a.model_dump() for a in body.add_nodes] if body.add_nodes else None,
        remove_node_ids=body.remove_node_ids,
    )
    return ApiResponse(message="任务图已确认")


@router.post("/missions/{mission_id}/replan", response_model=ApiResponse)
async def replan_mission(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    if mission.status not in ("awaiting_confirm", "executing", "draft"):
        from app.core.exceptions import BadRequestError
        raise BadRequestError("任务当前状态不可重拆", "errors.mission.invalid_state")
    await mission_service.replan_mission(db, mission)
    return ApiResponse(message="已重新拆解")


@router.post("/missions/{mission_id}/cancel", response_model=ApiResponse)
async def cancel_mission(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    await mission_service.cancel_mission(db, mission, user=user)
    return ApiResponse(message="任务已取消")


@router.delete("/missions/{mission_id}", response_model=ApiResponse)
async def delete_mission(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """删除对话：任意状态可删；活跃态先置 cancelled 再全链软删（节点/事件/产物一并不可见）。"""
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    await mission_service.delete_mission(db, mission, user=user)
    return ApiResponse(message="对话已删除")


@router.post("/missions/{mission_id}/priority", response_model=ApiResponse)
async def set_priority(
    mission_id: str,
    body: PriorityRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """P2 优先级插队：urgent 任务在同实例队列中排在 normal 之前（不中断运行中的节点）。"""
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    priority = 1 if body.level == "urgent" else 0
    await mission_service.set_mission_priority(db, mission, priority, user=user)
    return ApiResponse(message="优先级已设置")


# ── 工作流模板（设计 §二/§三） ─────────────────────────────────────

@router.post("/missions/{mission_id}/save-as-template", response_model=ApiResponse)
async def save_mission_as_template(
    mission_id: str,
    body: SaveAsTemplateRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """验收通过/已归档的任务保存为工作流模板（DAG 快照）。"""
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    if mission.status not in ("acceptance", "archived"):
        from app.core.exceptions import BadRequestError
        raise BadRequestError("仅验收通过或已归档的任务可保存为模板", "errors.mission.invalid_state")
    await mission_service.accept_mission(
        db, mission, user=user,
        save_as_template={"name": body.name, "description": body.description},
    )
    return ApiResponse(message="已保存为工作流模板")


@router.get("/workspaces/{workspace_id}/mission-templates", response_model=ApiResponse)
async def list_mission_templates(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    await wm_service.check_workspace_member(workspace_id, user, db)
    from app.services.mission import template_service
    templates = await template_service.list_templates(db, workspace_id)
    return ApiResponse(data=[{
        "id": t.id, "name": t.name, "description": t.description,
        "mission_type": t.mission_type, "execution_mode": t.execution_mode,
        "usage_count": t.usage_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for t in templates])


@router.post("/mission-templates/{template_id}/run", response_model=ApiResponse)
async def run_mission_template(
    template_id: str,
    body: TemplateRunRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """从模板实例化 Mission（跳过 LLM 拆解，直接建节点 → awaiting_confirm）。"""
    user, org = ctx
    from app.services.mission import template_service
    template = await template_service.get_template(db, template_id)
    await wm_service.check_workspace_member(template.workspace_id or "", user, db)
    mission = await template_service.instantiate_from_template(
        db, template, org=org, user=user,
        workspace_id=template.workspace_id or "",
        requirement_text=body.requirement_text,
        execution_mode=body.execution_mode,
    )
    return ApiResponse(data={"id": mission.id, "status": mission.status})


@router.delete("/mission-templates/{template_id}", response_model=ApiResponse)
async def delete_mission_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    from app.services.mission import template_service
    template = await template_service.get_template(db, template_id)
    if template.created_by != user.id and not user.is_super_admin:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("仅创建者或管理员可删除模板", "errors.mission.not_acceptor")
    template.soft_delete()
    await db.commit()
    return ApiResponse(message="模板已删除")


class ChatEditRequest(BaseModel):
    instruction: str  # 自然语言修改指令，如"把第2步标签改成data-analysis"


@router.post("/missions/{mission_id}/chat-edit", response_model=ApiResponse)
async def chat_edit_nodes(
    mission_id: str,
    body: ChatEditRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """对话式节点修改：LLM 解析自然语言指令并直接应用到 pending 节点。

    仅 awaiting_confirm 状态可用；修改后发 system_note 事件，用户看结果满意再点确认。
    """
    from app.core.exceptions import BadRequestError
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    if mission.status != "awaiting_confirm":
        raise BadRequestError("仅待确认状态可修改节点", "errors.mission.invalid_state")

    from app.services.mission.chat_edit_service import parse_and_apply_edit
    summary = await parse_and_apply_edit(db, mission, body.instruction, user=user)
    return ApiResponse(message=summary)


@router.post("/missions/{mission_id}/review-continue", response_model=ApiResponse)
async def review_continue(
    mission_id: str,
    body: ReviewContinueRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """step_review 模式的节点审核：approve 继续 / reject 打回 / pause 保持暂停。"""
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)

    from sqlalchemy import update as sa_update
    from app.services.mission.event_service import MissionEventService

    if body.action == "approve":
        await db.execute(sa_update(Mission).where(
            Mission.id == mission.id,
        ).values(paused_for_review=False))
        await MissionEventService(db).append(
            mission.id, org_id=mission.org_id,
            event_type="system_note", actor_type="user",
            actor_id=user.id, actor_name=getattr(user, "name", None),
            content="审核通过，继续执行",
        )
        await db.commit()
        return ApiResponse(message="已继续执行")
    elif body.action == "reject":
        if not body.node_id:
            from app.core.exceptions import BadRequestError
            raise BadRequestError("打回需指定 node_id", "errors.mission.invalid_state")
        node = await db.get(MissionNode, body.node_id)
        if node is None or node.mission_id != mission.id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("节点不存在", "errors.mission.node_not_found")
        node.status = "pending"
        node.attempt_count = 0
        await db.execute(sa_update(Mission).where(
            Mission.id == mission.id,
        ).values(paused_for_review=False))
        await MissionEventService(db).append(
            mission.id, org_id=mission.org_id, node_id=node.id,
            event_type="system_note", actor_type="user",
            actor_id=user.id, actor_name=getattr(user, "name", None),
            content=f"节点「{node.title}」已打回重做",
        )
        await db.commit()
        return ApiResponse(message="节点已打回重做")
    elif body.action == "pause":
        await db.execute(sa_update(Mission).where(
            Mission.id == mission.id,
        ).values(paused_for_review=True))
        await db.commit()
        return ApiResponse(message="已保持暂停")
    else:
        from app.core.exceptions import BadRequestError
        raise BadRequestError("无效操作", "errors.mission.invalid_state")


@router.post("/missions/{mission_id}/nodes/{node_id}/retry", response_model=ApiResponse)
async def retry_node(
    mission_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    node = await db.get(MissionNode, node_id)
    if node is None or node.mission_id != mission.id:
        raise NotFoundError("节点不存在", "errors.mission.node_not_found")
    await mission_service.retry_node(db, mission, node)
    return ApiResponse(message="节点已重试")


@router.post("/missions/{mission_id}/nodes/{node_id}/reassign", response_model=ApiResponse)
async def reassign_node(
    mission_id: str,
    node_id: str,
    body: ReassignRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    node = await db.get(MissionNode, node_id)
    if node is None or node.mission_id != mission.id:
        raise NotFoundError("节点不存在", "errors.mission.node_not_found")
    await mission_service.reassign_node(db, mission, node, body.instance_id, user=user)
    return ApiResponse(message="节点已改派")


@router.post("/missions/{mission_id}/questions/{event_id}/answer", response_model=ApiResponse)
async def answer_question(
    mission_id: str,
    event_id: str,
    body: QuestionAnswerRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    event = await db.get(MissionEvent, event_id)
    if event is None or event.mission_id != mission.id:
        raise NotFoundError("事件不存在", "errors.mission.event_not_found")
    await mission_service.answer_question(db, mission, event, body.answer, user=user)
    return ApiResponse(message="已回答")


@router.get("/missions/{mission_id}/events", response_model=ApiResponse)
async def list_mission_events(
    mission_id: str,
    after_seq: int = Query(0),
    kinds: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    kind_list = [k.strip() for k in kinds.split(",")] if kinds else None
    events = await mission_service.get_events(
        db, mission.id, after_seq=after_seq, kinds=kind_list,
    )
    return ApiResponse(data=[{
        "id": e.id, "seq": e.seq, "event_type": e.event_type,
        "node_id": e.node_id, "actor_type": e.actor_type,
        "actor_name": e.actor_name, "visibility": e.visibility,
        "content": e.content, "payload": e.payload,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in events])


@router.get("/missions/{mission_id}/events/stream")
async def mission_events_stream(
    mission_id: str,
    after_seq: int = Query(0),
    user=Depends(_query_user_dep()),
    db: AsyncSession = Depends(get_db),
):
    """SSE 事件流（EventSource 无法带 Authorization 头，鉴权走 query token，
    同 workspaces events 路由）。P1 用 2s 轮询增量推送。
    """
    mission = await mission_service.get_mission(db, mission_id)
    await wm_service.check_workspace_member(mission.workspace_id, user, db)
    last_seq = after_seq

    async def stream():
        nonlocal last_seq
        from app.core.deps import async_session_factory
        try:
            yield f"data: {json.dumps({'event': 'connected', 'mission_id': mission_id})}\n\n"
            while True:
                async with async_session_factory() as poll_db:
                    events = await mission_service.get_events(
                        poll_db, mission_id, after_seq=last_seq,
                    )
                for e in events:
                    last_seq = max(last_seq, e.seq)
                    yield (
                        "event: mission:event\n"
                        f"data: {json.dumps({'seq': e.seq, 'event_type': e.event_type, 'node_id': e.node_id, 'actor_type': e.actor_type, 'actor_name': e.actor_name, 'content': e.content, 'payload': e.payload, 'created_at': e.created_at.isoformat() if e.created_at else None}, ensure_ascii=False)}\n\n"
                    )
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    from fastapi.responses import StreamingResponse
    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/missions/{mission_id}/artifacts", response_model=ApiResponse)
async def list_artifacts(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    arts = (await db.execute(
        select(MissionArtifact)
        .where(MissionArtifact.mission_id == mission.id, not_deleted(MissionArtifact))
        .order_by(MissionArtifact.created_at.desc())
    )).scalars().all()
    return ApiResponse(data=[{
        "id": a.id, "node_id": a.node_id, "name": a.name, "kind": a.kind,
        "size_bytes": a.size_bytes, "version": a.version,
        "retention": a.retention,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in arts])


@router.post("/missions/{mission_id}/artifacts/{artifact_id}/promote", response_model=ApiResponse)
async def promote_artifact(
    mission_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    art = await db.get(MissionArtifact, artifact_id)
    if art is None or art.mission_id != mission.id:
        raise NotFoundError("产物不存在", "errors.mission.artifact_not_found")
    await mission_service.promote_artifact(db, mission, art, user=user)
    return ApiResponse(message="产物已保留")


@router.post("/missions/{mission_id}/accept", response_model=ApiResponse)
async def accept_mission(
    mission_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    await _require_accept_right(db, mission, user)
    await mission_service.accept_mission(db, mission, user=user)
    return ApiResponse(message="验收通过")


@router.post("/missions/{mission_id}/reject", response_model=ApiResponse)
async def reject_mission(
    mission_id: str,
    body: MissionRejectRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, _org = ctx
    mission = await _load_mission(db, mission_id, user)
    await _require_accept_right(db, mission, user)
    await mission_service.reject_mission(
        db, mission, reason=body.reason, rejected_nodes=body.rejected_nodes, user=user,
    )
    return ApiResponse(message="已打回")
