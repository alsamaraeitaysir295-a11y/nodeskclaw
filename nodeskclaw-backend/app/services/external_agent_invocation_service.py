"""外部 Agent tool 型插件调用历史的记录与查询服务。

数据流：invoke 端点（function 级 + Phase 1 兼容代理）在每次调用结束后写一条
ExternalAgentInvocation；用户侧表单页通过 GET /{agent_id}/invocations 拉自己的
最近 N 条，点击后用 result_data 重放完整结果。

安全边界：list_invocations 强制 (org_id, user_id) 过滤，用户只能看到自己的
调用记录，绝不跨用户 / 跨组织泄露。
"""

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.external_agent_invocation import ExternalAgentInvocation

logger = logging.getLogger(__name__)

# result_data 上限（50KB）：超过则截断为提示对象，避免大响应撑爆 DB
MAX_RESULT_BYTES = 50 * 1024
# params_summary 上限（字符）：仅历史列表展示用
MAX_PARAMS_SUMMARY_CHARS = 2000
# 历史列表单次最大条数（防恶意 limit 拖库）
MAX_LIST_LIMIT = 50


def summarize_params(params: dict[str, Any] | None) -> str | None:
    """参数 JSON 摘要（仅用户本人历史展示；超长截断）。

    空参数返回 None；序列化失败（极端不可 JSON 化的值）也返回 None，
    摘要属于锦上添花，不值得为它让 invoke 主流程报错。
    """
    if not params:
        return None
    try:
        text = json.dumps(params, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(text) <= MAX_PARAMS_SUMMARY_CHARS:
        return text
    return text[:MAX_PARAMS_SUMMARY_CHARS] + "...(truncated)"


def cap_result_data(result: dict[str, Any]) -> dict[str, Any] | None:
    """截取完整 invoke 响应供历史重放；超过 50KB 时降级为截断提示对象。

    - 剔除 resolved_items（可由 data + items_path 推导，属于冗余传输）；
    - 超限时保留 success 语义 + truncated 标记，前端据此提示「结果过大仅保留摘要」；
    - 不可 JSON 化的结果返回 None（不阻塞记录本身）。
    """
    if not isinstance(result, dict):
        return None
    slim = {k: v for k, v in result.items() if k != "resolved_items"}
    try:
        size = len(json.dumps(slim, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    if size <= MAX_RESULT_BYTES:
        return slim
    return {
        "success": slim.get("success") is not False,
        "truncated": True,
        "size_bytes": size,
        "display": "json",
        "data": {"truncated": True},
    }


async def record_invocation(
    *,
    agent_id: str,
    org_id: str,
    user_id: str,
    function_id: str | None,
    function_name: str,
    params: dict[str, Any] | None,
    result: dict[str, Any],
    db: AsyncSession,
) -> None:
    """invoke 结束后落一条调用历史（成功 / 上游失败 / 不可达均记录）。

    历史写入失败只记 warning 并回滚，绝不影响 invoke 主响应——历史是附属
    功能，不能因为它把用户的调用结果变成 500。
    """
    try:
        upstream_status = result.get("upstream_status")
        latency_ms = result.get("latency_ms")
        error = result.get("error")
        row = ExternalAgentInvocation(
            agent_id=agent_id,
            org_id=org_id,
            user_id=user_id,
            function_id=function_id,
            function_name=function_name or "unknown",
            params_summary=summarize_params(params),
            success=result.get("success") is not False and bool(result.get("success")),
            upstream_status=upstream_status if isinstance(upstream_status, int) else None,
            latency_ms=latency_ms if isinstance(latency_ms, int) else None,
            result_data=cap_result_data(result),
            error_message=(str(error)[:500] if error else None),
        )
        db.add(row)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to record invocation history (agent=%s user=%s): %s",
            agent_id, user_id, exc,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


async def list_invocations(
    *,
    agent_id: str,
    org_id: str,
    user_id: str,
    limit: int = 20,
    db: AsyncSession,
) -> list[ExternalAgentInvocation]:
    """列出用户在某插件下的最近 N 条调用历史（created_at 倒序）。

    强制 (agent_id, org_id, user_id) 三重过滤：org 隔离 + 用户只见自己。
    limit 收敛到 [1, 50]。
    """
    safe_limit = max(1, min(limit, MAX_LIST_LIMIT))
    result = await db.execute(
        select(ExternalAgentInvocation)
        .where(
            ExternalAgentInvocation.agent_id == agent_id,
            ExternalAgentInvocation.org_id == org_id,
            ExternalAgentInvocation.user_id == user_id,
            not_deleted(ExternalAgentInvocation),
        )
        .order_by(ExternalAgentInvocation.created_at.desc())
        .limit(safe_limit)
    )
    return list(result.scalars().all())
