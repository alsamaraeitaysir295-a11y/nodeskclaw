"""实例维度直聊服务（AI 员工双模式需求 2026-08-31）。

不加入协作空间的实例可作单智能体直用；进入空间的实例照旧参与空间聊天与任务编排。
会话/消息复用 Conversation / WorkspaceMessage（workspace_id=NULL 行，见两模型的注释）；
存量查询全部带 workspace_id 条件，NULL 行零影响。
"""
import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.conversation import Conversation
from app.models.workspace_message import WorkspaceMessage

# 直聊会话 member 形态：[instance_id, user_id]（is_manual=True，workspace_id=NULL）
DIRECT_CHAT_NAME = "一对一对话"


async def list_direct_conversations(
    db: AsyncSession, instance_id: str, current_user_id: str,
) -> list[Conversation]:
    """实例直聊会话列表（按当前用户隔离，防跨用户泄露）。"""
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    rows = (await db.execute(
        select(Conversation).where(
            Conversation.workspace_id.is_(None),
            Conversation.is_manual.is_(True),
            Conversation.member_node_ids.contains(cast([instance_id], JSONB)),
            Conversation.member_node_ids.contains(cast([current_user_id], JSONB)),
            not_deleted(Conversation),
        ).order_by(Conversation.last_message_at.desc().nulls_last())
    )).scalars().all()
    return list(rows)


async def create_direct_conversation(
    db: AsyncSession, instance_id: str, user_id: str,
) -> Conversation:
    """创建实例直聊会话（workspace 维度手动会话的同构实现，UUID hash 唯一）。"""
    conv = Conversation(
        workspace_id=None,
        name=DIRECT_CHAT_NAME,
        is_blackboard_group=False,
        is_manual=True,
        member_node_ids=[instance_id, user_id],
        member_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest()[:16],
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def get_direct_conversation_messages(
    db: AsyncSession, conversation_id: str, limit: int = 100,
) -> list[WorkspaceMessage]:
    """直聊会话历史（conversation 维度，与空间无关）。"""
    rows = (await db.execute(
        select(WorkspaceMessage).where(
            WorkspaceMessage.conversation_id == conversation_id,
            WorkspaceMessage.workspace_id.is_(None),
            not_deleted(WorkspaceMessage),
        ).order_by(WorkspaceMessage.created_at.asc()).limit(limit)
    )).scalars().all()
    return list(rows)


def build_direct_chat_prompt(
    agent_display_name: str,
    user_display_name: str,
    recent_messages: list[WorkspaceMessage],
) -> str:
    """直聊轻量系统提示词：无办公室成员/过道/黑板/@提及路由，保留身份与文件读取指引。"""
    lines = [
        f"你是 DeskClaw 平台的 AI 员工「{agent_display_name}」，正在与用户 {user_display_name} 进行一对一对话。",
        "- 直接、清晰地回答问题；这是一对一对话，无需 @提及任何人",
        "- 需要查看文件时用 read 工具：附件以路径形式给出（workspace/attachments/ 下），直接 read 该路径",
        "- 已安装的技能位于 .openclaw/skills/{技能名}/SKILL.md，需要时 read 后按技能说明执行",
    ]
    if recent_messages:
        lines.append("近期对话：")
        for m in recent_messages[-20:]:
            who = m.sender_name or m.sender_id[:8]
            lines.append(f"[{who}]: {m.content[:200]}")
    return "\n".join(lines)
