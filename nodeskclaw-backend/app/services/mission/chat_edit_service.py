"""对话式节点修改：LLM 解析自然语言指令，直接应用到 awaiting_confirm 的节点。

设计（2026-09-03 用户需求：确认走聊天方式，包括修改）：
- 用户在确认页输入"把第2步标签改成data-analysis，第3步加字数要求"
- 服务端把当前节点列表 + 指令发给 LLM，要求输出 JSON 编辑指令
- 解析后直接修改节点（pending 状态），发 system_note 事件记录改了什么
- 用户看结果满意再点"确认执行"（或继续聊天修改）
"""
import json
import logging

from sqlalchemy import select

from app.core.exceptions import BadRequestError
from app.models.base import not_deleted
from app.models.mission import Mission
from app.models.mission_node import MissionNode
from app.services.mission.event_service import MissionEventService
from app.services.mission.orchestrator import org_default_chat

logger = logging.getLogger(__name__)

_EDIT_PROMPT = """你是任务节点编辑助手。根据用户的自然语言指令修改以下任务节点。

当前节点列表（JSON）：
{nodes}

用户指令：{instruction}

请输出严格的 JSON（不要 markdown、不要多余文字），格式如下：
{{
  "edits": [
    {{"seq": 0, "action": "update", "title": "新标题（可选）", "capability_tags": ["新标签"], "acceptance_criteria": "新验收标准（可选）"}},
    {{"seq": 1, "action": "delete"}},
    {{"action": "add", "title": "新节点标题", "capability_tags": ["标签"], "acceptance_criteria": "验收标准"}}
  ]
}}

规则：
- seq 从 0 开始，对应节点列表中的 seq 字段
- action 只能是 update / delete / add
- update 只改传入的字段，不传的字段保持不变
- delete 不能删被依赖的节点（depends_on 引用了它的节点）
- add 不填 depends_on（新节点无依赖）
- capability_tags 必须来自这些标签：{tags}
- 输出中只能有 edits 数组，不能有其他字段
- 如果指令无法理解或不需要修改，输出 {{"edits": []}}"""


async def parse_and_apply_edit(db, mission: Mission, instruction: str, *, user) -> str:
    """解析自然语言指令并应用到节点，返回修改摘要。"""
    nodes = (await db.execute(
        select(MissionNode).where(
            MissionNode.mission_id == mission.id, not_deleted(MissionNode),
        ).order_by(MissionNode.seq)
    )).scalars().all()
    if not nodes:
        raise BadRequestError("无节点可修改", "errors.mission.no_nodes")

    # 可用能力词表
    from app.services.mission.orchestrator import load_active_capability_tags
    tags = await load_active_capability_tags(db, mission.org_id)
    tag_names = [t["tag"] for t in tags] if tags else ["backend", "frontend", "document-writing"]

    nodes_json = json.dumps([
        {
            "seq": n.seq,
            "title": n.title,
            "capability_tags": n.capability_tags or [],
            "acceptance_criteria": n.acceptance_criteria or "",
            "depends_on": n.depends_on or [],
        }
        for n in nodes
    ], ensure_ascii=False)

    prompt = _EDIT_PROMPT.format(
        nodes=nodes_json,
        instruction=instruction,
        tags=", ".join(tag_names),
    )

    # 调 LLM 解析
    from app.services.mission.orchestrator import _extract_first_json_block, DecompositionError
    try:
        raw = await org_default_chat(db, mission.org_id, prompt)
        parsed = _extract_first_json_block(raw)
    except DecompositionError:
        return "无法理解指令，请换个说法或直接点确认执行"

    edits = parsed.get("edits", [])
    if not edits:
        return "未做修改（指令可能无需变更）"

    # 应用编辑
    node_by_seq = {n.seq: n for n in nodes}
    depended_seqs = {d for n in nodes for d in (n.depends_on or [])}
    summary_parts: list[str] = []
    max_seq = max(n.seq for n in nodes) if nodes else -1

    for edit in edits:
        action = edit.get("action")
        if action == "update":
            seq = edit.get("seq")
            node = node_by_seq.get(seq)
            if not node or node.status != "pending":
                continue
            if edit.get("title"):
                node.title = edit["title"]
            if edit.get("capability_tags"):
                node.capability_tags = [t for t in edit["capability_tags"] if t in tag_names]
            if edit.get("acceptance_criteria"):
                node.acceptance_criteria = edit["acceptance_criteria"]
            summary_parts.append(f"修改#{seq}「{node.title}」")
        elif action == "delete":
            seq = edit.get("seq")
            node = node_by_seq.get(seq)
            if not node or node.status != "pending":
                continue
            if seq in depended_seqs:
                summary_parts.append(f"#{seq}被依赖，跳过删除")
                continue
            node.soft_delete()
            summary_parts.append(f"删除#{seq}「{node.title}」")
        elif action == "add":
            max_seq += 1
            import uuid
            node_id = str(uuid.uuid4())
            db.add(MissionNode(
                id=node_id,
                mission_id=mission.id,
                org_id=mission.org_id,
                seq=max_seq,
                title=edit.get("title") or f"新节点{max_seq}",
                description="",
                acceptance_criteria=edit.get("acceptance_criteria") or "",
                capability_tags=[t for t in (edit.get("capability_tags") or []) if t in tag_names],
                session_key=f"{mission.org_id}:{mission.id}:{node_id}",
            ))
            summary_parts.append(f"添加#{max_seq}「{edit.get('title', '')}」")

    if not summary_parts:
        return "未做修改"

    summary = "已修改：" + "；".join(summary_parts)
    await MissionEventService(db).append(
        mission.id, org_id=mission.org_id,
        event_type="system_note", actor_type="user",
        actor_id=user.id, actor_name=getattr(user, "name", None),
        content=f"指令「{instruction}」→ {summary}",
    )
    await db.commit()
    logger.info("chat-edit: mission=%s → %s", mission.id, summary)
    return summary
