"""外部 Agent 聊天会话与消息的 CRUD 服务层。"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.external_agent import ExternalAgent
from app.models.external_agent_chat import ExternalAgentChatSession, ExternalAgentMessage
from app.models.organization import Organization
from app.services import external_agent_adapter, external_agent_service


async def _load_org_allowed_cidrs(org_id: str, db: AsyncSession) -> list[str]:
    """加载组织的 SSRF 白名单（与 API 层 _get_org_allowed_cidrs 同语义，避免漏点）。"""
    result = await db.execute(
        select(Organization.external_agent_allowed_cidrs).where(Organization.id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return []
    return list(row)

logger = logging.getLogger(__name__)


async def create_session(
    *,
    agent_id: str,
    org_id: str,
    user_id: str,
    db: AsyncSession,
    title: str | None = None,
) -> ExternalAgentChatSession:
    """创建空会话（title 待首条消息写入后自动填充）。

    rag_standard + session_managed_by=="external" 的 Agent：创建平台会话后同步
    向外部 RAG 注册映射，建立 plat_{id} → 外部 session_id 的持久化关系，遵循
    方案附录 A 隔离规则 1-2；外部会话注册失败抛 BadRequestError，由调用方决定
    是否回滚平台会话。其余协议 / 非 external 模式：仅建平台会话，
    external_session_id 留空。
    """
    # 在一个事务内预占一个会话记录，拿 id 后才能拼 plat_ 前缀
    session = ExternalAgentChatSession(
        agent_id=agent_id, org_id=org_id, user_id=user_id,
        title=title,
    )
    db.add(session)
    await db.flush()  # 拿到 session.id，但事务尚未提交

    # 查 Agent 决定是否走"外部会话映射"分支
    agent = await _get_agent_for_mapping(agent_id=agent_id, org_id=org_id, db=db)
    if (
        agent is not None
        and agent.protocol == "rag_standard"
        and agent.session_managed_by == "external"
    ):
        external_sid = external_agent_adapter.compute_platform_external_session_id(session.id)
        api_key = external_agent_service.get_decrypted_api_key(agent)
        allowed_cidrs = await _load_org_allowed_cidrs(org_id, db)
        try:
            resolved = await external_agent_adapter.create_external_session(
                endpoint=agent.endpoint,
                external_session_id=external_sid,
                title=title or "新会话",
                api_key=api_key,
                allowed_cidrs=allowed_cidrs,
            )
        except Exception as exc:
            # 外部映射建立失败：回滚平台会话（事务里还没 commit）
            await db.rollback()
            raise
        session.external_session_id = resolved

    await db.commit()
    await db.refresh(session)
    return session


async def _get_agent_for_mapping(
    *, agent_id: str, org_id: str, db: AsyncSession
) -> ExternalAgent | None:
    """加载 Agent 用于会话映射分流；仅在 create_session 内部使用。"""
    result = await db.execute(
        select(ExternalAgent).where(
            ExternalAgent.id == agent_id,
            ExternalAgent.org_id == org_id,
            ExternalAgent.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_sessions(
    *, agent_id: str, user_id: str, db: AsyncSession
) -> list[ExternalAgentChatSession]:
    """列出用户在指定 Agent 下的所有会话，按 updated_at 倒序。"""
    result = await db.execute(
        select(ExternalAgentChatSession)
        .where(
            ExternalAgentChatSession.agent_id == agent_id,
            ExternalAgentChatSession.user_id == user_id,
            not_deleted(ExternalAgentChatSession),
        )
        .order_by(ExternalAgentChatSession.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_session(
    *, session_id: str, user_id: str, agent_id: str | None = None, db: AsyncSession
) -> ExternalAgentChatSession | None:
    """按 id 查询会话，同时校验归属用户；传入 agent_id 时还校验会话归属该 Agent。

    不传 agent_id 仅用于历史兼容场景；所有新调用点都应传入 agent_id，避免
    用户把自己在 Agent A 下的 session_id 传给 Agent B 的聊天端点，导致跨
    Agent 复用会话历史（历史消息、附件都会被当作这个不相关 Agent 的上下文）。
    """
    conditions = [
        ExternalAgentChatSession.id == session_id,
        ExternalAgentChatSession.user_id == user_id,
        not_deleted(ExternalAgentChatSession),
    ]
    if agent_id is not None:
        conditions.append(ExternalAgentChatSession.agent_id == agent_id)
    result = await db.execute(select(ExternalAgentChatSession).where(*conditions))
    return result.scalar_one_or_none()


async def delete_session(
    *, session_id: str, user_id: str, agent_id: str | None = None, db: AsyncSession
) -> None:
    """软删除会话（设置 deleted_at）。

    rag_standard + external session_managed_by 且 external_session_id 非空时：
    软删除后**额外**调一次外部 DELETE /api/v1/agent/history/{sid}，但失败仅
    记录日志、不阻塞本地软删除（用户已经在本地看不到该会话，外部清理失败
    应留待后续清理任务而非阻断主流程）。
    """
    session = await get_session(session_id=session_id, user_id=user_id, agent_id=agent_id, db=db)
    if not session:
        return

    # 先取出 Agent 与 external_session_id，软删除后再做外部清理——
    # 顺序无关（外部清理是 fire-and-forget），但先读出来可以避免后续事务
    # 影响 ORM 对象的加载。
    agent_id_for_call = session.agent_id
    external_sid_to_cleanup = session.external_session_id
    org_id_for_call = session.org_id

    session.deleted_at = datetime.now(timezone.utc)
    await db.commit()

    if external_sid_to_cleanup and agent_id_for_call and org_id_for_call:
        agent_result = await db.execute(
            select(ExternalAgent).where(
                ExternalAgent.id == agent_id_for_call,
                ExternalAgent.org_id == org_id_for_call,
                ExternalAgent.deleted_at.is_(None),
            )
        )
        agent = agent_result.scalar_one_or_none()
        if agent and agent.protocol == "rag_standard" and agent.session_managed_by == "external":
            api_key = external_agent_service.get_decrypted_api_key(agent)
            allowed_cidrs = await _load_org_allowed_cidrs(org_id_for_call, db)
            await external_agent_adapter.delete_external_history(
                endpoint=agent.endpoint,
                external_session_id=external_sid_to_cleanup,
                api_key=api_key,
                allowed_cidrs=allowed_cidrs,
            )


async def get_messages(
    *, session_id: str, db: AsyncSession
) -> list[ExternalAgentMessage]:
    """按时间升序返回会话内全部消息。"""
    result = await db.execute(
        select(ExternalAgentMessage)
        .where(ExternalAgentMessage.session_id == session_id)
        .order_by(ExternalAgentMessage.created_at.asc())
    )
    return list(result.scalars().all())


async def save_messages(
    *,
    session_id: str,
    user_content: str,
    user_attachments: list[dict] | None,
    assistant_content: str,
    assistant_thinking: str | None = None,
    db: AsyncSession,
) -> None:
    """批量写入用户消息和 Agent 响应，同时更新 session 的 updated_at 和 title。"""
    db.add(ExternalAgentMessage(
        session_id=session_id,
        role="user",
        content=user_content,
        attachments=user_attachments or None,
    ))
    db.add(ExternalAgentMessage(
        session_id=session_id,
        role="assistant",
        content=assistant_content,
        thinking=assistant_thinking or None,
    ))

    result = await db.execute(
        select(ExternalAgentChatSession).where(ExternalAgentChatSession.id == session_id)
    )
    chat_session = result.scalar_one_or_none()
    if chat_session:
        chat_session.updated_at = datetime.now(timezone.utc)
        if not chat_session.title and user_content:
            chat_session.title = user_content[:50]

    await db.commit()
