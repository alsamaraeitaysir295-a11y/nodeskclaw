"""聊天消息意图检测：判断用户消息是"任务需求"还是"普通对话"。

Phase 2（2026-09-03）：协作空间全聊天式任务流——用户不@任何人时，
编排者判断意图：任务 → 创建 Mission 并推卡片到聊天流；对话 → 转给最合适员工。
"""
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mission.orchestrator import org_default_chat

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """判断用户消息是"任务需求"还是"普通对话"。

任务需求的特征：
- 有明确目标和交付物（"输出一份报告"、"做个分析"、"整理一下"）
- 需要多步骤完成
- 用词偏向指令/请求（"帮我"、"请"、"生成"、"创建"）

普通对话的特征：
- 提问/闲聊/快速查询
- 不需要多步骤
- 用词偏向疑问/讨论

用户消息：{message}

输出严格 JSON：{{"intent": "task" | "chat", "reason": "一句话理由"}}"""


async def classify_intent(
    db: AsyncSession, org_id: str, message: str,
) -> str:
    """返回 "task" 或 "chat"。LLM 调用失败时降级为 "chat"（不误创建任务）。"""
    from app.services.mission.orchestrator import _extract_first_json_block
    try:
        prompt = _CLASSIFY_PROMPT.format(message=message[:500])
        raw = await org_default_chat(db, org_id, prompt)
        result = _extract_first_json_block(raw)
        intent = result.get("intent", "chat")
        if intent in ("task", "chat"):
            return intent
        return "chat"
    except Exception:
        logger.warning("意图检测失败，降级为 chat", exc_info=True)
        return "chat"
