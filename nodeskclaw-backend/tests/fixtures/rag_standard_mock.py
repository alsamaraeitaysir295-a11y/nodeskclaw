"""rag_standard 协议的最小 Mock 服务（Phase 1 任务 #11 验收替身）。

实现方案 ee/docs/外部智能体一期方案.md
附录 A 规定的全部端点契约：
  - POST /api/v1/agent/stream         → text/event-stream, delta / done / error
  - GET  /api/v1/agent/sessions?limit=N → 会话列表（连通性探活用）
  - POST /api/v1/agent/sessions        → 登记新会话（回传 session_id）
  - GET  /api/v1/agent/history/{sid}?limit=N → 单会话历史
  - DELETE /api/v1/agent/history/{sid} → 删除会话（200, 模拟成功）

设计决策：
  - 进程内字典（_SESSIONS）保存所有会话与消息；不写磁盘、无外部依赖，
    进程退出即清零，与真实生产 RAG 服务的"内存级一致性"语义对齐。
  - 已知 sid 不命中时返回 404，**用以驱动适配器的 ExternalSessionLostError
    自动重建路径**（spec 附录 A 隔离规则 4）的端到端验证。
  - 流式响应按字符切片逐条推送，模拟真实增量行为；不可在单 chunk 内
    一次性推完整文本，否则"流式 + 持久化兜底"的 happy path 退化。
  - 该 mock 独立于平台主应用，不依赖 settings / DB / 安全层；可作为
    进程独立运行（手动 E2E）或在 pytest fixture 内嵌启动（自动化）。

使用：
    uv run python tests/fixtures/rag_standard_mock.py --port 4099

手动 curl 试调：
    curl http://127.0.0.1:4099/api/v1/agent/sessions?limit=1
    curl -X POST http://127.0.0.1:4099/api/v1/agent/sessions \
         -H 'Content-Type: application/json' \
         -d '{"session_id":"plat_demo","title":"试调"}'
    curl -X POST http://127.0.0.1:4099/api/v1/agent/stream \
         -H 'Content-Type: application/json' \
         -d '{"question":"热压缺陷原因？","session_id":"plat_demo","mode":"auto"}'
    curl http://127.0.0.1:4099/api/v1/agent/history/plat_demo?limit=10
    curl -X DELETE http://127.0.0.1:4099/api/v1/agent/history/plat_demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query as QueryParam, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("rag_standard_mock")

app = FastAPI(title="rag_standard Mock", version="1.0")

# ── 进程内状态 ──────────────────────────────────────────────────────────────
# _SESSIONS[sid] = {"title": str, "created_at": float, "messages": [dict]}
_SESSIONS: dict[str, dict[str, Any]] = {}

# 记录每个端点被调用的次数 / 最近一次 body，便于 E2E 断言
# key = (method, path-pattern)
_CALL_LOG: dict[tuple[str, str], int] = defaultdict(int)
_LAST_REQUEST: dict[tuple[str, str], dict[str, Any]] = {}

# 模拟流式 chunk 间隔（秒），0 表示立即推完；可调大验证前端流式体验
_STREAM_CHUNK_DELAY = 0.0

# 模拟思考过程文案，便于 E2E 验证 thinking 事件能透传到前端
_MOCK_THINKING = (
    "正在检索知识库中关于「热压缺陷」的相关文档..."
    "命中 3 篇候选文档，综合分析后形成结论。"
)

# 模拟主回答的最小正文（E2E 拿这个判定"问题 → 答案"是否串起来）
# 长度尽量与一段真实 RAG 答案相当，覆盖多行 / code / list 等 markdown 元素
_MOCK_ANSWER_TEMPLATE = """针对你提出的「{question}」，结合知识库检索结果，整理答复如下：

1. **主要原因**是工艺参数偏差与原料水分控制不稳；
2. 推荐检查项：
   - 热压温度（±5℃ 范围内）
   - 加压时长（按板厚计算）
   - 原料含水率（建议 6-8%）
3. 如需更细化诊断，可提交样品编号与现场录像。

示例代码片段（供查询接口使用）：
```bash
curl -X POST $endpoint/api/v1/diagnose \\
  -H 'Content-Type: application/json' \\
  -d '{{"sample_id": "HP-2026-08-19-001"}}'
```

> 注：以上结论基于最近 90 天内的同类缺陷统计；新批次需重新采样分析。"""


def _record_call(method: str, path_pattern: str, body: Any | None) -> None:
    """登记一次 HTTP 调用，便于 E2E 断言。"""
    key = (method, path_pattern)
    _CALL_LOG[key] += 1
    if body is not None:
        _LAST_REQUEST[key] = body


# ── 连通性探活 ──────────────────────────────────────────────────────────────


@app.get("/api/v1/agent/sessions")
async def list_sessions(limit: int = QueryParam(20, ge=1, le=100)) -> list[dict[str, Any]]:
    """列出已登记的会话（连通性探活端点；真实 RAG 服务 GET 此端点 200 即视为可达）。

    E2E 通过 ?limit=1 探活时，响应必须是合法 JSON 数组且 200，否则
    probe_chat_endpoint 会判不可达。
    """
    _record_call("GET", "/api/v1/agent/sessions", {"limit": limit})
    return [
        {
            "session_id": sid,
            "title": meta["title"],
            "turn_count": len(meta["messages"]) // 2,
            "last_active": meta.get("updated_at", meta["created_at"]),
        }
        for sid, meta in list(_SESSIONS.items())[:limit]
    ]


# 注：以下端点在真实 RAG 服务里是 GET ……/sessions 返回数组；FastAPI 默认
# 不允许同一路径两种声明共存，所以 limit 参数必须放在查询字符串。已用
# QueryParam 形式在上面声明；下面是 POST 创建会话。


@app.post("/api/v1/agent/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    """登记新会话（创建平台会话时同步调用）。

    body: {"session_id": str, "title": str}
    返回: {"session_id": str, "title": str, "created_at": float}
    真实 RAG 服务会原样回传 session_id；本 mock 同样按字面回传，便于
    平台 `effective_external_sid == response.session_id` 的断言通过。

    注：用 Request.body() + json.loads 手动解析；与 FastAPI ``Body(...)``
    自动解析相比，行为对所有客户端（含 curl、httpx、requests）一致。
    """
    raw_bytes = await request.body()
    try:
        body = json.loads(raw_bytes) if raw_bytes else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _record_call("POST", "/api/v1/agent/sessions", body)
    sid = body.get("session_id")
    title = body.get("title", "")
    if not isinstance(sid, str) or not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    if sid not in _SESSIONS:
        _SESSIONS[sid] = {
            "title": title or sid,
            "created_at": time.time(),
            "updated_at": time.time(),
            "messages": deque(maxlen=200),
        }
    else:
        # 重复创建（重建外部会话时）：更新 title 与时间戳，清空 messages。
        _SESSIONS[sid]["title"] = title or _SESSIONS[sid]["title"]
        _SESSIONS[sid]["updated_at"] = time.time()
        _SESSIONS[sid]["messages"].clear()
    return {
        "session_id": sid,
        "title": _SESSIONS[sid]["title"],
        "created_at": _SESSIONS[sid]["created_at"],
    }


# ── 流式问答（SSE） ──────────────────────────────────────────────────────────


@app.post("/api/v1/agent/stream")
async def stream_chat(request: Request) -> StreamingResponse:
    """模拟真实 RAG 服务的 SSE 流。

    body: {"question": str, "session_id": str, "mode": "auto"}

    行为：
      - 若 session_id 不在 _SESSIONS 中：返回 404（让上层触发自动重建路径）。
      - 否则先把 question 记入该会话历史，再按字符切片推 delta 事件，
        最后 yield done（携带完整 answer 作为持久化兜底）。
      - 也支持"未知 session_id"以验证自动重建分支。

    思考过程放在第一个 delta 之前的特殊 "thinking" 事件中，前端折叠显示；
    此实现与 adapter 中 _parse_rag_standard_sse 的 delta / message 分支
    解耦——直接用裸文本（无 JSON 包装）来触发"无 type 字段"的兜底路径，
    验证多形态解析器均能正确产出消息片段。
    """
    raw_bytes = await request.body()
    try:
        body = json.loads(raw_bytes) if raw_bytes else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    _record_call("POST", "/api/v1/agent/stream", body)
    question = body.get("question", "")
    sid = body.get("session_id", "")

    if not sid or sid not in _SESSIONS:
        # 真实 RAG 服务"会话不存在"在生产有两种表达：HTTP 404（最直接）和
        # SSE error 事件 {type:'error', error:'session not found'}（更宽容）。
        # mock 走 HTTP 404 —— 适配器会先在 HTTP 阶段 raise ExternalSessionLostError，
        # 走重建路径，与 spec 附录 A 隔离规则 4 的快速通道对齐。
        raise HTTPException(status_code=404, detail=f"session {sid} not found")

    # 模拟"thinking + answer"两段：先 thinking 事件流，再主回答 delta 流
    answer = _MOCK_ANSWER_TEMPLATE.format(question=question or "（无问题）")
    session = _SESSIONS[sid]

    async def event_stream() -> AsyncIterator[str]:
        try:
            # 1. 推理链路片段（裸文本兜底格式：无 JSON 包装）
            for chunk_text in _chunk_text(_MOCK_THINKING, size=12):
                yield f"data: {json.dumps({'type': 'thinking', 'text': chunk_text}, ensure_ascii=False)}\n\n"
                if _STREAM_CHUNK_DELAY:
                    await asyncio.sleep(_STREAM_CHUNK_DELAY)
                if await request.is_disconnected():
                    return

            # 2. 主回答 delta 片段（标准 JSON delta 事件）
            for chunk_text in _chunk_text(answer, size=10):
                yield f"data: {json.dumps({'type': 'delta', 'text': chunk_text}, ensure_ascii=False)}\n\n"
                if _STREAM_CHUNK_DELAY:
                    await asyncio.sleep(_STREAM_CHUNK_DELAY)
                if await request.is_disconnected():
                    return

            # 3. 收尾：done 携带完整 answer，作为持久化兜底
            yield f"data: {json.dumps({'type': 'done', 'answer': answer, 'cancelled': False}, ensure_ascii=False)}\n\n"

            # 写入历史（与真实 RAG 服务一致：user question + assistant answer）
            session["messages"].append({"role": "user", "content": question})
            session["messages"].append({"role": "assistant", "content": answer})
            session["updated_at"] = time.time()
        except asyncio.CancelledError:
            # 客户端断开，停止推送（避免泄漏协程）
            logger.info("SSE stream cancelled for session %s", sid)
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _chunk_text(text: str, size: int = 10) -> list[str]:
    """按 size 字符切分文本，便于模拟流式增量。"""
    if size <= 0 or len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


# ── 历史读取 / 删除 ──────────────────────────────────────────────────────────


@app.get("/api/v1/agent/history/{sid}")
async def get_history(sid: str, limit: int = QueryParam(10, ge=1, le=100)) -> dict[str, Any]:
    """读取会话历史（返回最近 limit 条；前端回看历史对话时调用）。

    未知 sid → 404，配合 adapter 的自动重建路径验证。
    """
    _record_call("GET", "/api/v1/agent/history/{sid}", {"sid": sid, "limit": limit})
    meta = _SESSIONS.get(sid)
    if not meta:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    history = list(meta["messages"])[-limit:]
    return {
        "session_id": sid,
        "title": meta["title"],
        "turn_count": len(meta["messages"]) // 2,
        "history": history,
    }


@app.delete("/api/v1/agent/history/{sid}")
async def delete_history(sid: str) -> dict[str, Any]:
    """删除会话（平台软删除会话时同步调用；mock 即从内存中忘记该 sid）。"""
    _record_call("DELETE", "/api/v1/agent/history/{sid}", {"sid": sid})
    if sid in _SESSIONS:
        del _SESSIONS[sid]
    return {"message": "ok", "session_id": sid}


# ── 调试端点（非协议契约；仅供 E2E 断言 / 手动排查） ─────────────────────────


@app.get("/_mock/stats")
async def mock_stats() -> dict[str, Any]:
    """导出调用统计与当前会话数，用于 E2E 断言或人工排错。"""
    return {
        "call_counts": {f"{m} {p}": c for (m, p), c in _CALL_LOG.items()},
        "last_requests": {f"{m} {p}": body for (m, p), body in _LAST_REQUEST.items()},
        "session_count": len(_SESSIONS),
        "session_ids": list(_SESSIONS.keys()),
    }


@app.post("/_mock/reset")
async def mock_reset() -> dict[str, str]:
    """清空所有会话与调用统计，让 E2E 测试间互不污染。"""
    _SESSIONS.clear()
    _CALL_LOG.clear()
    _LAST_REQUEST.clear()
    return {"status": "reset"}


# ── 进程入口 ────────────────────────────────────────────────────────────────


def main() -> None:
    """手动启动入口：uv run python tests/fixtures/rag_standard_mock.py --port 4099"""
    parser = argparse.ArgumentParser(description="rag_standard mock service")
    parser.add_argument("--port", type=int, default=4099, help="bind port (default: 4099)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--chunk-delay", type=float, default=0.0,
                        help="inter-chunk delay in seconds (default: 0 for fast tests)")
    args = parser.parse_args()

    global _STREAM_CHUNK_DELAY
    _STREAM_CHUNK_DELAY = args.chunk_delay

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
