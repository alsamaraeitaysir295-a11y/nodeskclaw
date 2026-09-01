"""任务空间隧道上行接入（设计 §7.2 / §8 / T8）。

处理实例经 tunnel 上报的 mission.* 消息：
- ack → 节点 acked；progress/report → 事件（首个进展时 acked→running）
- artifact → 落存储（upload_raw）+ 隔离区产物行 + 事件
- blocked → L1 记事件不阻塞；L2 节点与 Mission 置 blocked_question
- done → 节点 done + token 汇总（节点/任务两级累加）；全部 done → acceptance

幂等：按节点 last_task_id 定位；不符（旧派发）仅记日志丢弃。
request_agent 协商转发属 T7 插件联动，P1 降级为 L1 处理并留 TODO。
"""
import base64
import binascii
import logging
from datetime import timedelta

from sqlalchemy import select, update

from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_node import MissionNode
from app.models.mission_org_config import MissionOrgConfig
from app.services.mission.event_service import MissionEventService

logger = logging.getLogger(__name__)

# 设计 §7.2：单产物超 20MB 拒收（base64 解码峰值约 27MB 内存）
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024

# done 可从这些活跃态直接完成（错过 ack/progress 的直通场景）；
# blocked_question 也允许——Agent 先阻塞后自行完成时，完成即解除本节点的阻塞
_DONE_STATES = ("dispatched", "acked", "running", "blocked_question")
_ACTIVE_STATES = ("dispatched", "acked", "running")


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def handle_mission_message(instance_id: str, payload: dict, *, session_factory=None) -> None:
    """mission.* 上行消息统一入口（collaboration_service 转发）。"""
    if session_factory is None:
        from app.core.deps import async_session_factory as session_factory
    msg_type = str(payload.get("type", ""))
    if not msg_type.startswith("mission."):
        return
    task_id = str(payload.get("task_id", ""))
    data = payload.get("data") or {}

    async with session_factory() as db:
        node = (await db.execute(
            select(MissionNode).where(
                MissionNode.last_task_id == task_id, not_deleted(MissionNode),
            )
        )).scalar_one_or_none()
        if node is None:
            logger.info("mission 消息无对应节点（旧派发/未知 task_id=%s type=%s）", task_id, msg_type)
            return
        mission = await db.get(Mission, node.mission_id)
        if mission is None:
            return
        svc = MissionEventService(db)

        if msg_type == "mission.task.ack":
            await _on_ack(db, svc, mission, node, instance_id)
        elif msg_type in ("mission.task.progress", "mission.task.report"):
            await _on_progress(db, svc, mission, node, msg_type, data)
        elif msg_type == "mission.task.artifact":
            await _on_artifact(db, svc, mission, node, data)
        elif msg_type == "mission.task.blocked":
            await _on_blocked(db, svc, mission, node, data)
        elif msg_type == "mission.task.done":
            await _on_done(db, svc, mission, node, data)
        else:
            logger.warning("未知 mission 消息类型 %s（task=%s）", msg_type, task_id)
            return
        await db.commit()


async def _on_ack(db, svc, mission, node, instance_id: str) -> None:
    """收到任务包：dispatched → acked（重复 ack 幂等跳过）。"""
    res = await db.execute(
        update(MissionNode)
        .where(MissionNode.id == node.id, MissionNode.status == "dispatched")
        .values(status="acked", acked_at=_utcnow())
    )
    if res.rowcount:
        await svc.append(
            mission.id, org_id=mission.org_id, node_id=node.id,
            event_type="task_ack", actor_type="agent",
            content=f"已接收任务（实例 {instance_id[:8]}…）",
            visibility="timeline",
        )


async def _on_progress(db, svc, mission, node, msg_type: str, data: dict) -> None:
    """progress=阶段性进展 / report=Agent 主动叙事；首个进展时 acked → running。"""
    message = str(data.get("message", "")).strip()
    if not message:
        return
    await db.execute(
        update(MissionNode)
        .where(MissionNode.id == node.id, MissionNode.status == "acked")
        .values(status="running", started_at=_utcnow())
    )
    event_type = "progress" if msg_type.endswith("progress") else "narrative"
    phase = data.get("phase")
    await svc.append(
        mission.id, org_id=mission.org_id, node_id=node.id,
        event_type=event_type, actor_type="agent",
        content=message, visibility="timeline",
        payload={"phase": phase} if phase else None,
    )


async def _on_artifact(db, svc, mission, node, data: dict) -> None:
    """产物：base64 解码 → upload_raw → 隔离区行（TTL）→ 事件。"""
    import uuid

    from app.services.storage_service import upload_raw

    name = str(data.get("name", "")).strip()
    if not name:
        logger.warning("mission 产物缺少 name（task=%s）", node.last_task_id)
        return
    size = int(data.get("size") or 0)
    content_b64 = str(data.get("content_base64") or "")
    if size > MAX_ARTIFACT_BYTES or len(content_b64) > MAX_ARTIFACT_BYTES:
        logger.warning("mission 产物超 20MB 拒收: name=%s size=%s", name, size)
        return
    try:
        content = base64.b64decode(content_b64) if content_b64 else b""
    except (binascii.Error, ValueError):
        logger.warning("mission 产物 base64 解码失败: name=%s", name)
        return

    artifact_id = str(uuid.uuid4())
    storage_key = f"missions/{mission.org_id}/{mission.workspace_id}/{mission.id}/{artifact_id}/{name}"
    try:
        await upload_raw(storage_key, content)
    except Exception:
        logger.warning("mission 产物存储失败: %s", storage_key, exc_info=True)
        return

    ttl_days = mission.artifact_ttl_days
    if ttl_days is None:
        cfg = (await db.execute(
            select(MissionOrgConfig).where(
                MissionOrgConfig.org_id == mission.org_id, not_deleted(MissionOrgConfig),
            )
        )).scalar_one_or_none()
        ttl_days = cfg.artifact_ttl_days if cfg else 14
    # ttl=0 即不留：直接不留存产物（设计 D6），仅记事件
    expires_at = _utcnow() + timedelta(days=ttl_days) if ttl_days > 0 else None

    artifact = MissionArtifact(
        id=artifact_id,
        mission_id=mission.id, node_id=node.id, org_id=mission.org_id,
        name=name, kind=str(data.get("kind") or "other"),
        storage_key=storage_key, size_bytes=len(content),
        retention="quarantine", expires_at=expires_at,
        produced_by_instance_id=node.assigned_instance_id,
    )
    db.add(artifact)
    await svc.append(
        mission.id, org_id=mission.org_id, node_id=node.id,
        event_type="artifact_produced", actor_type="agent",
        content=f"产出「{name}」" + ("（已进入临时保留区）" if expires_at else "（不留存）"),
        payload={"artifact_id": artifact_id, "name": name, "kind": artifact.kind,
                 "size_bytes": artifact.size_bytes},
    )


async def _on_blocked(db, svc, mission, node, data: dict) -> None:
    """执行者疑问：L1 记事件继续跑；L2 阻塞节点与任务等待人类。"""
    question = data.get("question") if isinstance(data.get("question"), dict) else {}
    level = str(question.get("level") or data.get("level") or "L1").upper()
    message = str(question.get("message") or data.get("reason") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if data.get("request_agent"):
        # TODO(T7): 转现有 agent 协商链路（design §8），P1 降级按 L1 处理
        logger.info("mission blocked 携带 request_agent，P1 降级 L1: %s", data.get("request_agent"))

    if level == "L2":
        await db.execute(
            update(MissionNode)
            .where(MissionNode.id == node.id, MissionNode.status.in_(_ACTIVE_STATES))
            .values(status="blocked_question")
        )
        await db.execute(
            update(Mission)
            .where(Mission.id == mission.id, Mission.status == "executing")
            .values(status="blocked_question")
        )
        await svc.append(
            mission.id, org_id=mission.org_id, node_id=node.id,
            event_type="l2_question", actor_type="agent",
            content=message or reason or "执行方提出阻塞问题",
            payload={"reason": reason, "assumption": question.get("assumption")},
        )
    else:
        await svc.append(
            mission.id, org_id=mission.org_id, node_id=node.id,
            event_type="l1_question", actor_type="agent",
            content=message or reason or "执行方提问（不阻塞）",
            payload={"reason": reason, "assumption": question.get("assumption")},
        )


async def _on_done(db, svc, mission, node, data: dict) -> None:
    """节点完成：token 累加（节点+任务两级）→ done；全部 done → acceptance。"""
    usage = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}
    prompt = int(usage.get("input") or 0)
    completion = int(usage.get("output") or 0)

    was_blocked = node.status == "blocked_question"
    res = await db.execute(
        update(MissionNode)
        .where(MissionNode.id == node.id, MissionNode.status.in_(_DONE_STATES))
        .values(
            status="done", finished_at=_utcnow(),
            prompt_token_cost=node.prompt_token_cost + prompt,
            completion_token_cost=node.completion_token_cost + completion,
            token_cost=node.token_cost + prompt + completion,
        )
    )
    if not res.rowcount:
        return  # 重复 done / 已终态：幂等跳过

    if was_blocked:
        # 阻塞源于本节点 → 完成即解除任务阻塞（token 保险丝的挂起不在此列）
        await db.execute(
            update(Mission)
            .where(Mission.id == mission.id, Mission.status == "blocked_question")
            .values(status="executing")
        )

    await db.execute(
        update(Mission)
        .where(Mission.id == mission.id)
        .values(
            prompt_token_cost=Mission.prompt_token_cost + prompt,
            completion_token_cost=Mission.completion_token_cost + completion,
            token_cost=Mission.token_cost + prompt + completion,
        )
    )
    summary = str(data.get("summary") or "").strip()
    await svc.append(
        mission.id, org_id=mission.org_id, node_id=node.id,
        event_type="node_done", actor_type="agent",
        content=summary or f"节点「{node.title}」完成",
        payload={"summary": summary, "tokens": {"prompt": prompt, "completion": completion}},
    )

    remaining = (await db.execute(
        select(MissionNode.id).where(
            MissionNode.mission_id == mission.id,
            MissionNode.status.notin_(("done", "skipped")),
            not_deleted(MissionNode),
        ).limit(1)
    )).scalar_one_or_none()
    if remaining is None:
        moved = (await db.execute(
            update(Mission)
            .where(Mission.id == mission.id, Mission.status == "executing")
            .values(status="acceptance")
        )).rowcount
        if moved:
            await svc.append(
                mission.id, org_id=mission.org_id,
                event_type="system_note", actor_type="system",
                content="全部节点完成，任务进入验收",
            )
