"""任务空间编排器 v1（设计 §4 / T3）。

职责：把用户需求文本拆解为受 JSON Schema 约束的任务 DAG（节点 + 能力标签 + 依赖）。

要点（设计 §4 + 附录 A T3a 验证结论）：
- 请求携带 response_format=json_object（OpenAI 兼容上游生效；Gemini 路径被
  proxy 白名单转换丢弃，无害），服务端始终执行"提取首个 JSON 块 + jsonschema
  校验"的兜底解析，不依赖单一机制
- 校验失败自动重试 1 次，再失败抛 DecompositionError（调用方落
  decomposition_failed，Mission 回 draft 允许重试拆解）
- 服务端做 DAG 环检测（拓扑排序）；标签必须来自给定词表
- LLM 调用经 chat 参数注入（ChatFn），测试传 fake、生产传 org_default_chat
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from jsonschema import ValidationError as JsonschemaValidationError
from jsonschema import validate as jsonschema_validate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.capability_tag import CapabilityTag
from app.models.org_llm_key import OrgModelProvider

logger = logging.getLogger(__name__)

ORCHESTRATOR_PROMPT_VERSION = "v1"
ORCHESTRATOR_SCHEMA_VERSION = "v1"

# 拆解输出 JSON Schema（服务端强校验；标题 ≤30 字符，节点 1-20 个）
DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["mission_title", "brief", "is_lightweight_candidate", "nodes", "escalation"],
    "properties": {
        "mission_title": {"type": "string", "minLength": 1, "maxLength": 30},
        "brief": {
            "type": "object",
            "required": ["goal", "constraints", "acceptance_criteria", "key_decisions"],
            "properties": {
                "goal": {"type": "string", "minLength": 1},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "key_decisions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "is_lightweight_candidate": {"type": "boolean"},
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "required": ["title", "description", "acceptance_criteria", "capability_tags", "depends_on"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                    "capability_tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "depends_on": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                },
            },
        },
        "escalation": {
            "type": "object",
            "required": ["l2_rules"],
            "properties": {"l2_rules": {"type": "array", "items": {"type": "string"}},
                           "l1_hint": {"type": "string"}},
        },
    },
}

ORCHESTRATOR_PROMPT_V1 = """你是任务编排器。把用户需求拆解为可独立验收的最小任务 DAG。

规则：
1. 每个节点是一个可独立验收的最小工作单元，有明确产出形式；不要把整段需求原样塞进一个节点
2. capability_tags 只能从下方能力词表中选择（用 tag 原文）；一个节点 1-3 个标签
3. 词表表达不了的子任务，选最接近的标签，并在 description 里说明能力缺口
4. depends_on 填上游节点在 nodes 数组中的下标（从 0 开始）；不允许成环、不允许自依赖
5. 简单到单人一步能完成的需求，is_lightweight_candidate 填 true 且只输出 1 个节点
6. escalation.l2_rules 列出该任务执行中必须阻塞等待人类的硬边界（如：删除不可恢复数据、修改任务结构、预算超限）

输出严格的 JSON（不要 markdown 代码块、不要多余文字），结构如下：
{
  "mission_title": "≤30 字的任务标题",
  "brief": {"goal": "目标", "constraints": ["约束"], "acceptance_criteria": ["验收标准"], "key_decisions": ["关键决策"]},
  "is_lightweight_candidate": false,
  "nodes": [{"title": "", "description": "", "acceptance_criteria": "", "capability_tags": ["tag"], "depends_on": []}],
  "escalation": {"l2_rules": ["..."]}
}

可用能力词表（tag | 中文名 | 语义）：
{vocab}

用户需求：
{requirement}"""

# chat 函数签名：prompt → completion 文本（测试注入 fake，生产传 org_default_chat）
ChatFn = Callable[[str], Awaitable[str]]


class DecompositionError(Exception):
    """拆解失败（解析/校验/环检测任一环节），调用方落 decomposition_failed。"""


@dataclass
class DecompositionNode:
    title: str
    description: str
    acceptance_criteria: str
    capability_tags: list[str]
    depends_on: list[int]


@dataclass
class DecompositionResult:
    mission_title: str
    brief: dict
    nodes: list[DecompositionNode]
    l2_rules: list[str]
    l1_hint: str | None
    mission_type: str  # MissionType.standard / lightweight
    coordinator_meta: dict = field(default_factory=dict)


def _build_prompt(requirement_text: str, tags: list[dict]) -> str:
    # 模板内含大量 JSON 花括号，不能用 str.format（会被当占位符），走显式替换
    vocab_lines = [
        f"- {t['tag']} | {t.get('label_zh') or ''} | {t.get('description') or ''}"
        for t in tags
    ]
    return (
        ORCHESTRATOR_PROMPT_V1
        .replace("{vocab}", "\n".join(vocab_lines))
        .replace("{requirement}", requirement_text)
    )


def _extract_first_json_block(text: str) -> dict:
    """从模型输出提取首个完整 JSON 对象（容忍 markdown 围栏/前后缀文本）。

    用 JSONDecoder.raw_decode 从每个 '{' 起试解，天然处理嵌套括号与字符串内花括号。
    """
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise DecompositionError("响应中未找到合法 JSON 对象")


def _validate_dag(nodes: list[DecompositionNode]) -> None:
    """服务端 DAG 校验：下标合法、无自依赖、无环（Kahn 拓扑排序）。"""
    n = len(nodes)
    for i, node in enumerate(nodes):
        for dep in node.depends_on:
            if not 0 <= dep < n:
                raise DecompositionError(f"节点 {i} 依赖下标越界: {dep}")
            if dep == i:
                raise DecompositionError(f"节点 {i} 自依赖")
    # Kahn：入度剥洋葱，剥不完即有环
    indegree = [0] * n
    downstream: list[list[int]] = [[] for _ in range(n)]
    for i, node in enumerate(nodes):
        for dep in node.depends_on:
            indegree[i] += 1
            downstream[dep].append(i)
    queue = [i for i, d in enumerate(indegree) if d == 0]
    peeled = 0
    while queue:
        cur = queue.pop()
        peeled += 1
        for nxt in downstream[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if peeled != n:
        raise DecompositionError("depends_on 存在环")


def _parse_and_validate(raw: str, vocab_tags: set[str]) -> DecompositionResult:
    """提取 → schema 校验 → 语义校验（词表/环）→ 组装结果。"""
    data = _extract_first_json_block(raw)
    try:
        jsonschema_validate(data, DECOMPOSITION_SCHEMA)
    except JsonschemaValidationError as e:
        raise DecompositionError(f"输出不符合 Schema: {e.message}") from e

    unknown = {
        tag for node in data["nodes"] for tag in node["capability_tags"]
        if tag not in vocab_tags
    }
    if unknown:
        raise DecompositionError(f"capability_tags 含词表外标签: {sorted(unknown)}")

    nodes = [
        DecompositionNode(
            title=n["title"],
            description=n.get("description", ""),
            acceptance_criteria=n.get("acceptance_criteria", ""),
            capability_tags=list(n["capability_tags"]),
            depends_on=list(n["depends_on"]),
        )
        for n in data["nodes"]
    ]
    _validate_dag(nodes)

    is_lightweight = bool(data["is_lightweight_candidate"]) and len(nodes) <= 1
    return DecompositionResult(
        mission_title=data["mission_title"],
        brief=data["brief"],
        nodes=nodes,
        l2_rules=list(data["escalation"].get("l2_rules", [])),
        l1_hint=data["escalation"].get("l1_hint"),
        mission_type="lightweight" if is_lightweight else "standard",
        coordinator_meta={
            "engine": "builtin",
            "prompt_version": ORCHESTRATOR_PROMPT_VERSION,
            "schema_version": ORCHESTRATOR_SCHEMA_VERSION,
        },
    )


async def decompose(
    requirement_text: str,
    capability_tags: list[dict],
    *,
    chat: ChatFn,
    max_attempts: int = 2,
) -> DecompositionResult:
    """需求文本 → 拆解结果。校验失败重试（默认共 2 次尝试），仍失败抛 DecompositionError。

    capability_tags: [{tag, label_zh, description}]（该 org 可用 active 词表）。
    """
    if not capability_tags:
        raise DecompositionError("能力词表为空，无法拆解")
    prompt = _build_prompt(requirement_text, capability_tags)
    vocab = {t["tag"] for t in capability_tags}

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        raw = await chat(prompt)
        try:
            return _parse_and_validate(raw, vocab)
        except DecompositionError as e:
            last_error = e
            logger.warning("拆解校验失败（第 %d 次）: %s", attempt, e)
    raise DecompositionError(f"拆解失败（重试后仍不合法）: {last_error}") from last_error


async def load_active_capability_tags(db: AsyncSession, org_id: str) -> list[dict]:
    """取该 org 可用词表（平台预置 + 本 org 扩展，active）。"""
    from sqlalchemy import or_

    rows = (await db.execute(
        select(CapabilityTag).where(
            not_deleted(CapabilityTag),
            CapabilityTag.status == "active",
            or_(CapabilityTag.org_id.is_(None), CapabilityTag.org_id == org_id),
        ).order_by(CapabilityTag.tag)
    )).scalars().all()
    return [
        {"tag": r.tag, "label_zh": r.label_zh, "description": r.description}
        for r in rows
    ]


# ── 默认 LLM 传输（生产用；v1 仅支持 openai-completions 型上游）──────────────


async def org_default_chat(db: AsyncSession, org_id: str, prompt: str) -> str:
    """用 org 首个 active 组织 Key 调一次 chat completion，返回文本。

    v1 传输范围：openai-completions 兼容上游（openai/openrouter/minimax-openai/
    自定义 base_url）；anthropic/gemini 型组织 Key 明确报不支持（走
    DecompositionError → decomposition_failed 路径，待后续任务扩展）。
    模型选择：allowed_models[0]，缺省回退 DEFAULT_TEST_MODELS。
    """
    from app.services.model_catalog_service import DEFAULT_TEST_MODELS, PROVIDER_BASE_URLS

    provider_row = (await db.execute(
        select(OrgModelProvider).where(
            not_deleted(OrgModelProvider),
            OrgModelProvider.org_id == org_id,
            OrgModelProvider.is_active.is_(True),
        ).order_by(OrgModelProvider.created_at).limit(1)
    )).scalar_one_or_none()
    if provider_row is None:
        raise DecompositionError("组织未配置可用的模型供应商 Key")

    api_type = provider_row.api_type or "openai-completions"
    if api_type != "openai-completions":
        raise DecompositionError(f"编排器 v1 暂不支持 {api_type} 型组织 Key")

    allowed = provider_row.allowed_models or []
    model = allowed[0] if allowed else DEFAULT_TEST_MODELS.get(provider_row.provider)
    if not model:
        raise DecompositionError(f"组织 Key（{provider_row.provider}）未配置可用模型")

    if provider_row.base_url:
        url = f"{provider_row.base_url.rstrip('/')}/chat/completions"
    else:
        base = PROVIDER_BASE_URLS.get(provider_row.provider)
        if not base:
            raise DecompositionError(f"供应商 {provider_row.provider} 缺少 Base URL 配置")
        url = f"{base.rstrip('/')}/v1/chat/completions"

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # OpenAI 兼容上游启用 JSON 模式；Gemini 路径经 proxy 会丢弃，服务端兜底解析
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {provider_row.api_key}"}
    try:
        async with httpx.AsyncClient(
            verify=not provider_row.skip_ssl_verify, timeout=180,
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise DecompositionError(f"编排器调用上游失败: {type(e).__name__}: {e}") from e
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise DecompositionError(f"上游响应结构异常: {e}") from e
