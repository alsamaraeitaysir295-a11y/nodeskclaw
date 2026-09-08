"""开放 Registry IP 限流单测：分桶计数、X-Forwarded-For、阈值来自 settings。"""

import pytest

from app.core import open_registry_rate_limit as rl


class _Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class _Client:
    def __init__(self, ip):
        self.host = ip


class _StubRequest:
    def __init__(self, ip="10.0.0.1", forwarded=None):
        self.headers = _Headers()
        if forwarded:
            self.headers["x-forwarded-for"] = forwarded
        self.client = _Client(ip)


@pytest.fixture(autouse=True)
def _reset():
    rl.reset_open_registry_rate_limits()
    yield
    rl.reset_open_registry_rate_limits()


def test_under_limit_passes():
    req = _StubRequest()
    for _ in range(3):
        rl.check_open_registry_rate_limit(req, bucket="read")


def test_over_limit_raises_429(monkeypatch):
    from app.core.exceptions import AppException

    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 3)
    req = _StubRequest()
    for _ in range(3):
        rl.check_open_registry_rate_limit(req, bucket="read")
    with pytest.raises(AppException) as ei:
        rl.check_open_registry_rate_limit(req, bucket="read")
    assert ei.value.status_code == 429


def test_buckets_are_independent(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1)
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT", 1)
    req = _StubRequest()
    rl.check_open_registry_rate_limit(req, bucket="read")
    rl.check_open_registry_rate_limit(req, bucket="download")
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(req, bucket="read")
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(req, bucket="download")


def test_forwarded_for_first_segment_used(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1)
    rl.check_open_registry_rate_limit(_StubRequest(forwarded="1.1.1.1, 2.2.2.2"))
    # 不同 IP 不受影响
    rl.check_open_registry_rate_limit(_StubRequest(forwarded="3.3.3.3, 2.2.2.2"))
    with pytest.raises(Exception):
        rl.check_open_registry_rate_limit(_StubRequest(forwarded="1.1.1.1, 9.9.9.9"))


def test_max_tracked_keys_raises_429(monkeypatch):
    """伪造大量不同 IP 无限造键：总键数超 _MAX_TRACKED_KEYS 后整体 429（内存兜底）。"""
    from app.core.exceptions import AppException

    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1000)
    monkeypatch.setattr(rl, "_MAX_TRACKED_KEYS", 2)
    # 3 个不同 IP 依次记账：检查时已有键数 0/1/2，均未超上限，放行
    for i in range(3):
        rl.check_open_registry_rate_limit(_StubRequest(ip=f"10.1.{i}.1"), bucket="read")
    # 第 4 个新 IP：已有 3 键 > 2 → 429
    with pytest.raises(AppException) as ei:
        rl.check_open_registry_rate_limit(_StubRequest(ip="10.1.9.9"), bucket="read")
    assert ei.value.status_code == 429


def test_empty_key_recycled_after_window(monkeypatch):
    """超窗后该 IP 的空时间戳列表被 prune 清空（键复用，不残留过期记录）。"""
    monkeypatch.setattr(rl.time, "monotonic", lambda: 1000.0)
    rl.check_open_registry_rate_limit(_StubRequest(ip="10.2.0.1"), bucket="read")
    assert rl._counters["read"]["10.2.0.1"] == [1000.0]
    # 时间前进超窗，同 IP 再来：旧时间戳全部过期被清，仅剩本次
    monkeypatch.setattr(rl.time, "monotonic", lambda: 1000.0 + rl._WINDOW_SECONDS + 1)
    rl.check_open_registry_rate_limit(_StubRequest(ip="10.2.0.1"), bucket="read")
    assert rl._counters["read"]["10.2.0.1"] == [1061.0]


def test_rejected_request_leaves_no_empty_key(monkeypatch):
    """被容量上限拒绝的新 IP 不在桶里残留空键（空键在 prune 后被 pop）。"""
    monkeypatch.setattr("app.core.config.settings.OPEN_REGISTRY_READ_RATE_LIMIT", 1000)
    monkeypatch.setattr(rl, "_MAX_TRACKED_KEYS", 1)
    # 检查时键数 0/1 均 <= 1，两个 IP 放行后共 2 键
    rl.check_open_registry_rate_limit(_StubRequest(ip="10.4.0.1"), bucket="read")
    rl.check_open_registry_rate_limit(_StubRequest(ip="10.4.0.2"), bucket="read")
    with pytest.raises(rl.OpenRegistryRateLimitError):
        rl.check_open_registry_rate_limit(_StubRequest(ip="10.4.0.3"), bucket="read")
    assert "10.4.0.3" not in rl._counters["read"]
    assert len(rl._counters["read"]) == 2
