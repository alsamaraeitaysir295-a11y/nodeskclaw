"""skill 使用埋点：从 LLM 请求/响应流解析模型发起的 tool_calls，
归因到实例已安装的 gene 后写 gene_market_events（event_type='skill_use'）。

背景：后端看不到 skill 真实执行（SKILL.md 注入提示词 + agent 内部 read），
而所有实例的 LLM 流量必经本 proxy——模型"决定调用某工具"的那一刻在这里可见。
口径（与产品确认，见 docs/技能市场统计功能方案.md）：
- read 工具且 path 指向 .openclaw/skills/{name}/ 下任意文件（SKILL.md、
  references/、子文档）→ 该 skill 被实际打开（skill 安装形态见 backend
  app/services/runtime/openclaw_gene_install_adapter.py，
  skill_name = manifest.skill.name 或 gene.slug）
- MCP 工具名带 server 前缀（如 mcp__{server}__{tool}）→ 按分段匹配
  InstanceMcpServer（source='gene' 且有 source_gene_id）归因
- 裸工具名（exec / 普通文件 read 等）无法唯一归因，跳过
局限：计"模型发起的调用"，工具执行失败也计；仅覆盖走 proxy 的流量；
模型仅凭 system prompt 里的技能摘要作答（未读技能文件、未调 MCP）的
"浅层使用"不计——使用榜语义是"深度使用"。

实现约束：表结构以后端 app/models/gene_market_event.py 为准，本模块用
原生 SQL 读写（避免在 proxy 里复制 ORM model 产生双源漂移）；写入走
asyncio.create_task 非阻塞，失败仅 warning。
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.database import get_session

logger = logging.getLogger(__name__)

# .openclaw/skills/{skill_name}/ 下任意文件的匹配（SKILL.md 本体、references/、
# 子文档等——模型只要真正读取技能内容就算一次使用；路径可能是相对/绝对）
_SKILL_PATH_RE = re.compile(r"\.openclaw/skills/([^/]+)/")

# 视为"读文件"的工具名（OpenClaw 主用 read，兼容常见变体）
_READ_TOOL_NAMES = {"read", "read_file"}

# 疑似 MCP 工具名的特征（mcp_ 前缀或 __ 分隔）：不匹配的直接跳过 MCP 归因，
# 避免普通工具（exec/browser/write...）也让每次 LLM 轮次白查一次库
_MCP_NAME_HINT_RE = re.compile(r"(?:^|_)mcp_|__")

# 单次响应最多记录的使用事件数（防异常超大 tool_calls 刷库）
_MAX_EVENTS_PER_RESPONSE = 20


class ToolCallCollector:
    """跨 SSE chunk 累积流式 tool_call 片段，或直接接收完整非流式响应。

    流式协议：choices[].delta.tool_calls[] 每个 {index, id?, function?:
    {name?, arguments 片段}}；同一 index 的 arguments 需拼接。
    非流式：choices[].message.tool_calls[] 已完整。
    """

    def __init__(self) -> None:
        # key = tool_call id 或 index；value = {"name": str, "arguments": str}
        self._calls: dict[str, dict] = {}
        self._order: list[str] = []
        # 上一个分片的 key：部分 provider（如 minimax 流式）的 arguments 后续
        # 分片不带 id/index，必须续写到同一条目，否则参数碎片会散落到垃圾条目
        self._last_key: str | None = None

    def feed_chunk(self, chunk: dict) -> None:
        """喂入一个已解析的 SSE data 对象（OpenAI 兼容流式增量）。"""
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                # key 规则：index 优先（同一调用的各分片 index 稳定，id 只在首片出现），
                # 无 index 用 id，两者都没有的续片归到上一个分片（流式按序到达）
                if tc.get("index") is not None:
                    key = str(tc["index"])
                elif tc.get("id"):
                    key = tc["id"]
                else:
                    key = self._last_key or "0"
                self._last_key = key
                if key not in self._calls:
                    self._calls[key] = {"name": "", "arguments": ""}
                    self._order.append(key)
                call = self._calls[key]
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if fn.get("name"):
                        call["name"] = fn["name"]
                    if fn.get("arguments"):
                        call["arguments"] += fn["arguments"]

    def feed_response(self, payload: dict) -> None:
        """喂入一个完整的 OpenAI 兼容非流式响应。"""
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            for tc in message.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                self._calls[str(tc.get("id") or uuid.uuid4())] = {
                    "name": fn["name"],
                    "arguments": fn.get("arguments") or "",
                }

    def complete_calls(self) -> list[dict]:
        """返回累积完成的 tool_calls（name + 已拼接 arguments）。"""
        return [
            {"name": c["name"], "arguments": c["arguments"]}
            for c in self._calls.values()
            if c["name"]
        ]


def _skill_name_from_call(name: str, arguments: str) -> str | None:
    """从 read 调用参数中提取 skill 名（读取技能目录下任意文件均算，匹配不到返回 None）。"""
    if name not in _READ_TOOL_NAMES:
        return None
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None
    path = args.get("path") or args.get("file_path") or args.get("filePath")
    if not isinstance(path, str):
        return None
    m = _SKILL_PATH_RE.search(path)
    return m.group(1) if m else None


def _gene_id_from_mcp_tool(name: str, mcp_servers: list[dict]) -> str | None:
    """MCP 工具名按 '_' 分段后命中已知 server 名 → 返回 source_gene_id。

    分段匹配同时覆盖 mcp__{server}__{tool} 与 {server}_{tool} 两种命名风格；
    server 名过短（<2）时跳过，避免误命中。
    """
    parts = {p for p in name.split("_") if p}
    for server in mcp_servers:
        server_name = (server.get("server") or "").strip()
        if len(server_name) < 2:
            continue
        if server_name in parts:
            return server["gene_id"]
    return None


async def _load_instance_gene_map(db, instance_id: str) -> dict[str, dict]:
    """该实例已安装 gene 的 {skill_name: {gene_id, slug, name}} 映射。

    skill_name = manifest.skill.name（缺省回退 gene.slug），与 backend
    openclaw_gene_install_adapter 的部署命名一致（同 get_instance_skills）。
    """
    rows = (await db.execute(text("""
        SELECT ig.gene_id, g.slug, g.name, g.manifest
        FROM instance_genes ig
        JOIN genes g ON g.id = ig.gene_id
        WHERE ig.instance_id = :iid AND ig.deleted_at IS NULL
          AND ig.status = 'installed' AND g.deleted_at IS NULL
    """), {"iid": instance_id})).all()
    mapping: dict[str, dict] = {}
    for gene_id, slug, name, manifest_raw in rows:
        skill_name = slug
        try:
            manifest = json.loads(manifest_raw) if manifest_raw else {}
            skill_name = (manifest.get("skill") or {}).get("name") or slug
        except (json.JSONDecodeError, TypeError):
            pass
        mapping[skill_name] = {"gene_id": gene_id, "slug": slug, "name": name}
    return mapping


async def _load_instance_mcp_servers(db, instance_id: str) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT name, source_gene_id FROM instance_mcp_servers
        WHERE instance_id = :iid AND deleted_at IS NULL
          AND source = 'gene' AND source_gene_id IS NOT NULL
    """), {"iid": instance_id})).all()
    return [{"server": r[0], "gene_id": r[1]} for r in rows]


async def record_skill_usage(instance: Any, calls: list[dict]) -> None:
    """归因并写入 skill_use 事件（调用方统一经 schedule_record 走后台任务）。"""
    if not calls:
        return
    try:
        # 预筛：SKILL.md read 命中，或任何可能走 MCP 归因的调用；两类都为空
        # 时尽早返回，普通对话请求不碰库
        skill_names = {
            sn for sn in (
                _skill_name_from_call(c["name"], c["arguments"]) for c in calls
            ) if sn
        }
        maybe_mcp = [
            c for c in calls
            if c["name"] not in _READ_TOOL_NAMES and _MCP_NAME_HINT_RE.search(c["name"])
        ]
        if not skill_names and not maybe_mcp:
            return

        gene_ids: set[str] = set()
        async with get_session() as db:
            if skill_names:
                gene_map = await _load_instance_gene_map(db, instance.id)
                for sn in skill_names:
                    gene = gene_map.get(sn)
                    if gene:
                        gene_ids.add(gene["gene_id"])
            if maybe_mcp:
                mcp_servers = await _load_instance_mcp_servers(db, instance.id)
                for c in maybe_mcp:
                    gene_id = _gene_id_from_mcp_tool(c["name"], mcp_servers)
                    if gene_id:
                        gene_ids.add(gene_id)
            if not gene_ids:
                return

            # 统一按 gene_id 回查 slug/name（覆盖 MCP 归因拿不到名称的场景）
            rows = (await db.execute(text("""
                SELECT id, slug, name FROM genes
                WHERE id = ANY(:gids) AND deleted_at IS NULL
            """), {"gids": list(gene_ids)})).all()

            now = datetime.now(timezone.utc)
            for gene_id, slug, name in rows[:_MAX_EVENTS_PER_RESPONSE]:
                await db.execute(text("""
                    INSERT INTO gene_market_events
                        (id, gene_id, gene_slug, gene_name, event_type,
                         user_id, org_id, created_at, updated_at)
                    VALUES (:id, :gid, :slug, :name, 'skill_use',
                            :uid, :oid, :now, :now)
                """), {
                    "id": str(uuid.uuid4()), "gid": gene_id,
                    "slug": slug, "name": name,
                    "uid": instance.created_by, "oid": instance.org_id,
                    "now": now,
                })
            await db.commit()
    except Exception:
        logger.warning(
            "skill usage 埋点失败 instance=%s", getattr(instance, "id", "?"),
            exc_info=True,
        )


def schedule_record(instance: Any, calls: list[dict]) -> None:
    """非阻塞调度埋点（调用方在请求热路径上，禁止直接 await）。"""
    if not calls:
        return
    asyncio.create_task(record_skill_usage(instance, calls))
