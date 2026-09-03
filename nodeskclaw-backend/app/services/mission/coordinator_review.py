"""编排者验收复核（主/子智能体协作，2026-09-03）。

子智能体显式 mission_complete 后，编排者（后端服务 + 组织 LLM）对照该节点的
验收标准复核完成总结与已交产物：通过 → node_done；不通过 → 打回重派
（反馈注入任务包，最多 MAX_REJECTIONS 次）；打回用尽仍不通过 → L2 人工确认。

fail-open：LLM 不可用 / 输出不可解析时默认通过——复核是质量增强，
不应因评审自身故障阻断任务流水线（此时退回"仅显式完成"语义）。
"""
import json
import logging
import re
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode

logger = logging.getLogger(__name__)

# 打回上限：超过后不再自动重派，转 L2 人工确认
MAX_REJECTIONS = 2

# L2 升级后人工回答的强制通过关键词（answer_question 侧消费）
FORCE_ACCEPT_KEYWORDS = ["通过", "同意", "接受", "认可"]

ChatFn = Callable[[str], Awaitable[str]]

REVIEW_PROMPT = """你是任务编排者，负责验收子智能体的工作产出。对照验收标准判断该节点是否真正完成。

[节点任务] {title}
[任务说明] {description}
[验收标准] {criteria}
[子智能体提交的完成总结]
{summary}
[已提交产物]
{artifacts}

判定要点：总结是否声称完成且内容与验收标准实质对应（而非进展播报/计划描述）；
要求产物时是否已提交；是否只完成了一部分。
只输出 JSON（不要 markdown 代码块、不要多余文字）：
{{"verdict": "pass 或 reject", "feedback": "reject 时给出具体、可执行的改进要求；pass 时为空串"}}"""


def _extract_verdict(raw: str) -> tuple[bool, str]:
    """解析复核输出。取第一个 JSON 块；verdict=reject 且 feedback 非空才算打回。"""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("复核输出无 JSON 块")
    data = json.loads(match.group(0))
    feedback = str(data.get("feedback") or "").strip()
    if data.get("verdict") == "reject" and feedback:
        return False, feedback[:1000]
    return True, ""


async def review_node_completion(
    db: AsyncSession,
    mission: Mission,
    node: MissionNode,
    summary: str,
    *,
    chat: ChatFn | None = None,
) -> tuple[bool, str]:
    """复核节点完成声明。返回 (通过?, 打回反馈)。

    chat 注入点供测试；生产默认 org_default_chat（组织首个 active Key）。
    """
    arts = (await db.execute(
        select(MissionArtifact.name, MissionArtifact.kind).where(
            MissionArtifact.node_id == node.id, not_deleted(MissionArtifact),
        )
    )).all()
    artifacts_desc = "\n".join(f"- {a.name}（{a.kind}）" for a in arts) or "（无）"

    prompt = REVIEW_PROMPT.format(
        title=node.title,
        description=node.description or "（无）",
        criteria=node.acceptance_criteria or "（无）",
        summary=(summary or "（空）")[:3000],
        artifacts=artifacts_desc,
    )
    if chat is None:
        from app.services.mission.orchestrator import org_default_chat
        chat = lambda p: org_default_chat(db, mission.org_id, p)  # noqa: E731

    try:
        raw = await chat(prompt)
        return _extract_verdict(raw)
    except Exception:
        logger.warning("编排者复核失败，按通过处理（fail-open）", exc_info=True)
        return True, ""


async def count_rejections(db: AsyncSession, node_id: str) -> int:
    return len((await db.execute(
        select(MissionEvent.id).where(
            MissionEvent.node_id == node_id,
            MissionEvent.event_type == "node_review_rejected",
            not_deleted(MissionEvent),
        )
    )).all())


async def collect_review_feedback(db: AsyncSession, node_id: str, mission_id: str) -> list[str]:
    """组装打回重派时的反馈注入：历次编排者打回 + 最近一次人工答复（若有）。

    供调度器任务包构造调用（review_feedback 字段 → 插件提示词渲染）。
    """
    feedback: list[str] = [str(ev.content) for ev in (await db.execute(
        select(MissionEvent).where(
            MissionEvent.node_id == node_id,
            MissionEvent.event_type == "node_review_rejected",
            not_deleted(MissionEvent),
        ).order_by(MissionEvent.seq)
    )).scalars().all() if ev.content]

    answered = (await db.execute(
        select(MissionEvent).where(
            MissionEvent.mission_id == mission_id,
            MissionEvent.event_type == "question_answered",
            not_deleted(MissionEvent),
        ).order_by(MissionEvent.seq.desc()).limit(1)
    )).scalars().first()
    if answered and answered.content:
        feedback.append(f"人工指示：{answered.content}")

    return feedback
