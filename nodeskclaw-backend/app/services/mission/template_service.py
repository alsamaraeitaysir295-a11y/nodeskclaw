"""任务空间工作流模板服务（设计 docs/任务空间工作流模板与编排设计.md §二）。

保存：accept_mission 事务内从 Mission + MissionNode 快照到模板。
实例化：直接按模板建 MissionNode（跳过 LLM 拆解），status=awaiting_confirm。
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, NotFoundError
from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_event import MissionEventCounter
from app.models.mission_node import MissionNode
from app.models.mission_template import MissionTemplate, MissionTemplateNode
from app.services.mission.event_service import MissionEventService

logger = logging.getLogger(__name__)


async def save_as_template(
    db: AsyncSession,
    mission: Mission,
    *,
    name: str,
    description: str,
    user,
) -> MissionTemplate:
    """从 Mission + MissionNode 快照创建工作流模板（accept_mission 事务内调用）。"""
    nodes = (await db.execute(
        select(MissionNode)
        .where(MissionNode.mission_id == mission.id, not_deleted(MissionNode))
        .order_by(MissionNode.seq)
    )).scalars().all()
    if not nodes:
        raise BadRequestError("任务无节点，无法保存为模板", "errors.mission.no_nodes")

    template = MissionTemplate(
        org_id=mission.org_id,
        workspace_id=mission.workspace_id,
        name=name,
        description=description,
        brief_template=mission.brief or {},
        escalation_policy=mission.escalation_policy,
        mission_type=mission.mission_type,
        execution_mode=mission.execution_mode or "auto",
        created_from_mission_id=mission.id,
        created_by=user.id,
    )
    db.add(template)
    await db.flush()  # 拿 template.id

    for node in nodes:
        db.add(MissionTemplateNode(
            id=str(uuid.uuid4()),
            template_id=template.id,
            seq=node.seq,
            title=node.title,
            description=node.description,
            acceptance_criteria=node.acceptance_criteria,
            capability_tags=list(node.capability_tags or []),
            depends_on=list(node.depends_on or []),
        ))

    logger.info("工作流模板已保存: %s (from mission=%s, %d nodes)", name, mission.id, len(nodes))
    return template


async def list_templates(
    db: AsyncSession, workspace_id: str,
) -> list[MissionTemplate]:
    """空间可用模板：空间级 + 组织级，active 状态。"""
    from sqlalchemy import or_
    return list((await db.execute(
        select(MissionTemplate).where(
            MissionTemplate.status == "active",
            not_deleted(MissionTemplate),
            or_(
                MissionTemplate.workspace_id == workspace_id,
                MissionTemplate.workspace_id.is_(None),
            ),
        ).order_by(MissionTemplate.created_at.desc())
    )).scalars().all())


async def get_template(db: AsyncSession, template_id: str) -> MissionTemplate:
    t = await db.get(MissionTemplate, template_id)
    if t is None or t.deleted_at is not None:
        raise NotFoundError("模板不存在", "errors.mission.template_not_found")
    return t


async def instantiate_from_template(
    db: AsyncSession,
    template: MissionTemplate,
    *,
    org, user, workspace_id: str,
    requirement_text: str,
    execution_mode: str | None = None,
) -> Mission:
    """从模板创建 Mission：直接按模板节点建 MissionNode，跳过 LLM 拆解。

    status 停在 awaiting_confirm（用户可微调后走现有 confirm 确认执行）。
    """
    # 复用 create_mission 的 draft 创建逻辑
    mission = Mission(
        org_id=org.id,
        workspace_id=workspace_id,
        title=template.name,
        requirement_text=requirement_text,
        created_by=user.id,
        status="awaiting_confirm",  # 跳过 draft/decomposing，直接待确认
        brief=template.brief_template or {},
        escalation_policy=template.escalation_policy,
        mission_type=template.mission_type,
        execution_mode=execution_mode or template.execution_mode or "auto",
        coordinator_meta={
            "engine": "template",
            "template_id": template.id,
            "template_name": template.name,
        },
    )
    db.add(mission)
    await db.flush()
    db.add(MissionEventCounter(mission_id=mission.id))

    # 按模板节点建 MissionNode
    template_nodes = (await db.execute(
        select(MissionTemplateNode)
        .where(MissionTemplateNode.template_id == template.id, not_deleted(MissionTemplateNode))
        .order_by(MissionTemplateNode.seq)
    )).scalars().all()

    for tn in template_nodes:
        node_id = str(uuid.uuid4())
        db.add(MissionNode(
            id=node_id,
            mission_id=mission.id,
            org_id=mission.org_id,
            seq=tn.seq,
            title=tn.title,
            description=tn.description,
            acceptance_criteria=tn.acceptance_criteria,
            capability_tags=list(tn.capability_tags or []),
            depends_on=list(tn.depends_on or []),
            session_key=f"{mission.org_id}:{mission.id}:{node_id}",
        ))

    # usage_count +1
    template.usage_count += 1

    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id,
        event_type="mission_created", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"从模板「{template.name}」创建任务",
        payload={"template_id": template.id, "node_count": len(template_nodes)},
    )
    await db.commit()
    await db.refresh(mission)
    logger.info("模板实例化: template=%s → mission=%s (%d nodes)",
                template.id, mission.id, len(template_nodes))
    return mission
