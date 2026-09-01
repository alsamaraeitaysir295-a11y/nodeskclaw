"""实例维度直聊 API（AI 员工双模式：不进空间可单智能体直用）。

与空间聊天链路（/workspaces/{ws}/agents/{id}/chat）并行存在、互不影响：
- 本组端点不校验 WorkspaceAgent（实例无需加入任何空间）
- 权限 = org 成员（get_current_org）+ 实例属于当前 org
- SSE 形态与空间聊天一致（data:{content} / [DONE] / data:{error}），前端可复用解析
"""
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import async_session_factory, get_current_org, get_db
from app.core.exceptions import BadRequestError, NotFoundError
from app.models.base import not_deleted
from app.models.conversation import Conversation
from app.models.instance import Instance
from app.services import direct_chat_service as dc_service
from app.services import workspace_message_service as msg_service
from app.schemas.common import ApiResponse

router = APIRouter()


class DirectChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


async def _load_direct_instance(
    db: AsyncSession, instance_id: str, org,
) -> Instance:
    """取实例并校验归属当前组织（不查 WorkspaceAgent——直聊不要求入空间）。"""
    inst = (await db.execute(
        select(Instance).where(Instance.id == instance_id, not_deleted(Instance))
    )).scalar_one_or_none()
    if inst is None:
        raise NotFoundError("AI 员工不存在", "errors.instance.not_found")
    if inst.org_id != org.id:
        raise NotFoundError("AI 员工不存在", "errors.instance.not_found")
    return inst


async def _load_owned_conversation(
    db: AsyncSession, instance_id: str, conversation_id: str, user_id: str,
) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if (
        conv is None or conv.deleted_at is not None
        or conv.workspace_id is not None
        or instance_id not in (conv.member_node_ids or [])
        or user_id not in (conv.member_node_ids or [])
    ):
        raise NotFoundError("会话不存在", "errors.conversation.not_found")
    return conv


@router.get("/{instance_id}/conversations")
async def list_conversations(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, org = ctx
    await _load_direct_instance(db, instance_id, org)
    rows = await dc_service.list_direct_conversations(db, instance_id, user.id)
    return ApiResponse(data=[{
        "id": c.id,
        "name": c.name,
        "member_node_ids": c.member_node_ids,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        "last_message_preview": c.last_message_preview,
    } for c in rows])


@router.post("/{instance_id}/conversations")
async def create_conversation(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, org = ctx
    await _load_direct_instance(db, instance_id, org)
    conv = await dc_service.create_direct_conversation(db, instance_id, user.id)
    return ApiResponse(data={"id": conv.id, "name": conv.name})


@router.get("/{instance_id}/conversations/{conversation_id}/messages")
async def list_messages(
    instance_id: str,
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    user, org = ctx
    await _load_direct_instance(db, instance_id, org)
    await _load_owned_conversation(db, instance_id, conversation_id, user.id)
    rows = await dc_service.get_direct_conversation_messages(db, conversation_id)
    return ApiResponse(data=[{
        "id": m.id,
        "sender_type": m.sender_type,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "content": m.content,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    } for m in rows])


@router.post("/{instance_id}/chat")
async def direct_chat(
    instance_id: str,
    data: DirectChatRequest,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(get_current_org),
):
    """与未入空间实例的一对一直聊（SSE，形态同空间聊天）。"""
    user, org = ctx
    inst = await _load_direct_instance(db, instance_id, org)

    conversation_id = data.conversation_id
    if conversation_id:
        await _load_owned_conversation(db, instance_id, conversation_id, user.id)
    else:
        conv = await dc_service.create_direct_conversation(db, instance_id, user.id)
        conversation_id = conv.id

    from app.services.tunnel import tunnel_adapter
    if instance_id not in tunnel_adapter.connected_instances:
        raise BadRequestError("AI 员工实例未通过隧道连接", "errors.workspace.agent_connection_missing")

    recent = await dc_service.get_direct_conversation_messages(db, conversation_id)
    agent_name = getattr(inst, "agent_display_name", None) or inst.name
    user_name = getattr(user, "name", None) or "用户"
    context_prompt = dc_service.build_direct_chat_prompt(agent_name, user_name, recent)

    # 先落用户消息（带 conversation_id 会同步刷新会话 last_message_at/preview）
    await msg_service.record_message(
        db,
        workspace_id=None,
        sender_type="user",
        sender_id=user.id,
        sender_name=user_name,
        content=data.message,
        message_type="private",
        target_instance_id=instance_id,
        conversation_id=conversation_id,
    )

    messages = [
        {"role": "system", "content": context_prompt},
        {"role": "user", "content": data.message},
    ]

    async def stream():
        full_response = ""
        try:
            # 会话隔离：借用 workspace 前缀机制构造 per-conversation 会话键
            chat_stream = await tunnel_adapter.send_chat_request(
                instance_id, messages,
                workspace_id=f"direct-{conversation_id}",
                stream=True,
            )
            async for chunk_msg in chat_stream:
                from app.services.tunnel.protocol import TunnelMessageType
                if chunk_msg.type == TunnelMessageType.CHAT_RESPONSE_ERROR:
                    yield f"data: {json.dumps({'error': chunk_msg.payload.get('error', 'unknown')})}\n\n"
                    break
                if chunk_msg.type == TunnelMessageType.CHAT_RESPONSE_DONE:
                    yield "data: [DONE]\n\n"
                    break
                content = chunk_msg.payload.get("content", "")
                if content:
                    full_response += content
                    yield f"data: {json.dumps({'content': content})}\n\n"
        except ConnectionError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        if full_response:
            async with async_session_factory() as save_db:
                await msg_service.record_message(
                    save_db,
                    workspace_id=None,
                    sender_type="agent",
                    sender_id=instance_id,
                    sender_name=agent_name,
                    content=full_response,
                    message_type="private",
                    conversation_id=conversation_id,
                )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
