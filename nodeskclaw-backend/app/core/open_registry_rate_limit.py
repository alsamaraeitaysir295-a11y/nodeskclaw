"""开放 Registry 匿名限流：IP 维度进程内滑动窗口（写法参照 auth_rate_limit.py）。

不引入 Redis；多副本下计数不跨 Pod 共享（日活 ~150 规模 1-2 副本可接受，
与登录限流同款限制）。阈值从 settings 读取，环境变量可调。
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request

from app.core.auth_rate_limit import get_client_ip
from app.core.config import settings
from app.core.exceptions import AppException

_WINDOW_SECONDS = 60.0

# _counters 总键数硬上限：X-Forwarded-For 可被伪造出无限新 IP，每个新 IP 都会
# 造新键；超上限后直接整体 429，作为内存兜底（正常流量远达不到该量级）
_MAX_TRACKED_KEYS = 10_000

# bucket -> ip -> 窗口期内时间戳列表（time.monotonic()）
_counters: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))


class OpenRegistryRateLimitError(AppException):
    def __init__(self):
        super().__init__(
            code=42900,
            message="请求过于频繁，请稍后再试",
            status_code=429,
            message_key="errors.common.too_many_attempts",
        )


def _limit_for(bucket: str) -> int:
    if bucket == "download":
        return settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT
    return settings.OPEN_REGISTRY_READ_RATE_LIMIT


def check_open_registry_rate_limit(request: Request, bucket: str = "read") -> None:
    """未超限即记账放行；超限抛 429。"""
    limit = _limit_for(bucket)
    key = get_client_ip(request)
    now = time.monotonic()
    bucket_counters = _counters[bucket]
    timestamps = bucket_counters[key]
    cutoff = now - _WINDOW_SECONDS
    timestamps[:] = [t for t in timestamps if t > cutoff]
    if not timestamps:
        # 该 IP 该桶已无剩余记录：先回收空键（被拒/未记账的请求不残留条目）
        bucket_counters.pop(key, None)
    if sum(len(ips) for ips in _counters.values()) > _MAX_TRACKED_KEYS:
        raise OpenRegistryRateLimitError()
    if len(timestamps) >= limit:
        raise OpenRegistryRateLimitError()
    timestamps.append(now)
    # 空键回收后 timestamps 已脱离桶，放行记账时需重新挂回
    bucket_counters[key] = timestamps


def reset_open_registry_rate_limits() -> None:
    """测试辅助：清空全部计数。"""
    _counters.clear()
