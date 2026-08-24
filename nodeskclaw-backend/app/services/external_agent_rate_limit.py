"""外部 Agent 插件化接入 — 单进程令牌桶限流（spec §9.5）。

设计目标
--------
- 默认每 (agent_id, user_id) 桶容量 10 个令牌、60 秒全量补满。
- 超限时由调用方决定上抛 TooManyRequestsError（HTTP 429）。
- 完全在进程内，不引入 Redis / DB 计数器 / 分布式锁。

已知局限（单副本限制）
----------------------
- 多副本部署下每个 Pod 持有自己的桶字典，事实上限将变为 ``副本数 × 容量``。
  多副本精确限流需要改用 Redis / DB 计数器，作为独立任务另行决策，
  本模块不在 invoke 链路工时内承担该改造（spec §9.5 明确划出范围）。

实现要点
--------
- 键空间：``(agent_id, user_id)`` 二元组；UUID 经 ``str(...).lower()`` 归一化
  避免大小写不一致造成的"同一用户不同桶"。
- 桶状态：``(tokens, last_refill_at)``；消费时按 elapsed × refill_rate 折算
  可用令牌数后扣 1，整个 check+consume 在 ``threading.Lock`` 内完成，避免
  FastAPI 跨工作线程调度造成计数错乱（参见
  ``app/core/auth_rate_limit.py`` 的同源模式）。
- TTL 驱逐：桶条目无活动 ``BUCKET_TTL`` 秒后下次访问触发惰性驱逐；测试
  也可显式调 ``reset_buckets()`` / ``_evict_expired(now)``。

本模块故意把"开 / 关 / 容量 / refill 周期"等参数作为模块级常量暴露，避免
增加 Settings 字段改动面（Settings 已有的字段不在本任务范围内）。生产运维
需要在紧急情况下关停时直接调 ``set_enabled(False)`` 即可；测试通过
``monkeypatch.setattr`` 同样能切换。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.core.exceptions import AppException

# ── 模块级默认参数 ────────────────────────────────────────────────────
#
# 这些常量是本模块的"事实来源"；通过模块属性可被 monkeypatch 覆盖。

DEFAULT_CAPACITY: int = 10           # 桶容量（令牌数）
DEFAULT_REFILL_SECONDS: float = 60.0 # 全量 refill 周期
DEFAULT_BUCKET_TTL: int = 300        # 桶无活动 TTL（秒），超时下次访问惰性驱逐

# 紧急逃生开关（运维 / E2E 用）：默认开启；调用 ``set_enabled(False)`` 或
# monkeypatch 此属性可全局禁用令牌桶检查。
ENABLED: bool = True

# ── 本模块私有异常：HTTP 429 错误 ──────────────────────────────────────


class TooManyRequestsError(AppException):
    """spec §9.5：超限后由全局 handler 序列化为 429 + i18n message_key。"""

    def __init__(self, *, retry_after: str | None = None) -> None:
        params = {"retry_after": retry_after} if retry_after else None
        super().__init__(
            code=42900,
            message="调用频率超出限制，请稍后再试",
            status_code=429,
            message_key="errors.external_agent.rate_limited",
            message_params=params,
        )


# ── 桶条目：本地数据结构，仅本模块使用 ────────────────────────────────


@dataclass
class _Bucket:
    tokens: float
    last_refill_at: float  # time.monotonic() 秒
    last_touch_at: float   # 任何访问（含读 / refill）都更新，便于 TTL 驱逐


# ── 进程内桶字典 + 锁 ────────────────────────────────────────────────
#
# 全局单例，跨请求共享；与 ``app/core/auth_rate_limit.py`` 的
# ``_failure_counters`` 同源思路。FastAPI 在多线程 sync 端点下要
# 保护字典读改写，因此用 ``threading.Lock`` 而非 ``asyncio.Lock``
# （asyncio.Lock 在跨线程同步代码里不会被正确触发）。

_buckets: dict[tuple[str, str], _Bucket] = {}
_buckets_lock = threading.Lock()


def _bucket_key(agent_id: str, user_id: str) -> tuple[str, str]:
    """归一化 (agent_id, user_id)：UUID 全小写，避免同一键被拆成多桶。"""
    return (str(agent_id).lower(), str(user_id).lower())


def _refill(bucket: _Bucket, *, now: float, capacity: float, refill_seconds: float) -> None:
    """按 elapsed 时间按比例补充令牌，但不超过 capacity。"""
    elapsed = max(0.0, now - bucket.last_refill_at)
    if refill_seconds <= 0:
        # 防御性：避免除零；视为一次性消耗（不再补充）。
        bucket.last_refill_at = now
        bucket.last_touch_at = now
        return
    refill_rate = capacity / refill_seconds  # tokens / second
    bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate)
    bucket.last_refill_at = now
    bucket.last_touch_at = now


def _evict_expired(now: float, ttl: float) -> None:
    """惰性驱逐：上次接触时间距今超过 ttl 的桶条目直接丢弃。"""
    if ttl <= 0:
        return
    cutoff = now - ttl
    expired = [k for k, b in _buckets.items() if b.last_touch_at < cutoff]
    for k in expired:
        _buckets.pop(k, None)


def set_enabled(enabled: bool) -> None:
    """紧急开关：运维可在线切换（不重启）；测试通过 monkeypatch.setattr 同样可用。"""
    global ENABLED
    ENABLED = bool(enabled)


async def check_rate_limit(
    agent_id: str,
    user_id: str,
    *,
    capacity: int | None = None,
    refill_seconds: float | None = None,
    bucket_ttl: int | None = None,
) -> None:
    """消耗 1 个令牌；桶空时抛 TooManyRequestsError（HTTP 429）。

    - 默认容量 / 周期由模块级常量控制（10 / 60s）；测试可显式传入覆盖。
    - ``ENABLED=False`` 时直接跳过（不抛错）。
    - 锁内串行化桶操作，避免跨线程计数错乱。
    """
    if not ENABLED:
        return

    cap = float(capacity if capacity is not None else DEFAULT_CAPACITY)
    period = float(refill_seconds if refill_seconds is not None else DEFAULT_REFILL_SECONDS)
    ttl = float(bucket_ttl if bucket_ttl is not None else DEFAULT_BUCKET_TTL)
    key = _bucket_key(agent_id, user_id)

    # ``threading.Lock`` 而非 ``asyncio.Lock`` —— FastAPI 同步依赖链
    # 在多线程事件循环里仍可能被工作线程持有，需可重入线程级锁。
    with _buckets_lock:
        now = time.monotonic()
        bucket = _buckets.get(key)
        if bucket is None:
            # 首次访问：起始满桶
            bucket = _Bucket(tokens=cap, last_refill_at=now, last_touch_at=now)
            _buckets[key] = bucket
        else:
            _refill(bucket, now=now, capacity=cap, refill_seconds=period)
            # 惰性驱逐：长时间不活动直接丢弃旧桶，下次访问重新以满桶开始。
            if bucket.last_touch_at + ttl < now:
                _buckets.pop(key, None)
                bucket = _Bucket(tokens=cap, last_refill_at=now, last_touch_at=now)
                _buckets[key] = bucket

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            bucket.last_touch_at = now
            return

        # 桶空：计算距下一个令牌可用的时间，附在 message_params 里方便前端展示。
        deficit = 1.0 - bucket.tokens
        # tokens / second = cap / period → seconds = deficit / (cap / period)
        wait_seconds = deficit / (cap / period) if cap > 0 and period > 0 else period
        bucket.last_touch_at = now

    # 在锁外 sleep-then-check 是不可取的（会阻塞请求线程），仅返回 429。
    raise TooManyRequestsError(retry_after=f"{wait_seconds:.1f}")


def reset_buckets() -> None:
    """清空所有桶 —— 主要供测试 / 运维紧急重置使用。"""
    with _buckets_lock:
        _buckets.clear()


def _evict_all_expired(now: float | None = None) -> int:
    """立即驱逐所有过期桶，返回被驱逐的数量（供测试断言 / 内部使用）。"""
    with _buckets_lock:
        before = len(_buckets)
        _evict_expired(now if now is not None else time.monotonic(), DEFAULT_BUCKET_TTL)
        return before - len(_buckets)


def _bucket_snapshot() -> dict[tuple[str, str], float]:
    """仅供测试：返回当前 (agent,user) → tokens 的浅拷贝快照。"""
    with _buckets_lock:
        return {k: v.tokens for k, v in _buckets.items()}
