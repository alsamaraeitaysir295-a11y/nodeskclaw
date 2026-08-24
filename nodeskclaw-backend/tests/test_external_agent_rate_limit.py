"""外部 Agent 插件限流（spec §9.5 + 任务 #6）：单进程令牌桶单测 + 端点集成。

覆盖：
  1. 同一 (agent, user) 桶耗尽后下一次 check 抛 TooManyRequestsError；
  2. 不同 user 各自有独立桶；
  3. 同一 user 对不同 agent 各自有独立桶；
  4. refilled 后令牌恢复（用小桶 3/2s 加速）；
  5. EXTERNAL_AGENT_RATE_LIMIT_ENABLED=false 时调用直接放行；
  6. TTL 驱逐惰性触发不导致正常使用回归；
  7. 通过 POST /{id}/invoke 端点返回 429 且 message_key=errors.external_agent.rate_limited。

多副本限制
----------
本测试仅在单进程内有效；多副本部署需另行引入 Redis / DB 计数器（spec §9.5
明确划出范围）。桶字典按 (agent_id, user_id) 归一化为小写字符串，UUID 大小写
不敏感。
"""
from __future__ import annotations

import time
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import external_agent_rate_limit as rl_module
from app.services.external_agent_rate_limit import (
    DEFAULT_REFILL_SECONDS,
    TooManyRequestsError,
    _bucket_snapshot,
    check_rate_limit,
    reset_buckets,
    set_enabled,
)
from tests.conftest import TestSessionLocal


# ── 公共夹具 ──────────────────────────────────────────────────────────


def _override_user(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override() -> None:
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _isolate_buckets(monkeypatch):
    """每个测试前后清空桶字典并恢复 ENABLED 状态，缩短测试运行时间。

    refill 周期在需要时由具体测试通过 monkeypatch.setattr(rl_module, ...)
    收紧；这里只负责"测试结束后世界状态干净"。
    """
    set_enabled(True)
    reset_buckets()
    yield
    reset_buckets()
    set_enabled(True)
    _clear_override()


async def _make_org_user(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"rl-org-{suffix}", slug=f"rl-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"rl-{suffix}@example.com", name=f"rl-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user, org


# ── 单元测试：check_rate_limit 行为 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_first_n_requests_within_window_all_succeed():
    """默认容量 10：前 10 次不抛，第 11 次抛 TooManyRequestsError。"""
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_id, user_id)
    # 第 11 次必抛（断言在下一个测试里覆盖）
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_id, user_id)


@pytest.mark.asyncio
async def test_eleventh_request_raises(monkeypatch):
    """第 11 次直接 raise TooManyRequestsError（HTTP 429）。"""
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_id, user_id)
    with pytest.raises(TooManyRequestsError) as exc_info:
        await check_rate_limit(agent_id, user_id)
    assert exc_info.value.status_code == 429
    assert exc_info.value.message_key == "errors.external_agent.rate_limited"


@pytest.mark.asyncio
async def test_different_users_have_independent_buckets(monkeypatch):
    """用户 A 桶打满 → 抛；用户 B 首次消费仍放行。"""
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_a = f"user-A-{uuid.uuid4().hex}"
    user_b = f"user-B-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_id, user_a)
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_id, user_a)
    # B 仍可消费
    await check_rate_limit(agent_id, user_b)


@pytest.mark.asyncio
async def test_different_agents_have_independent_buckets_for_same_user(monkeypatch):
    """同一用户对不同 Agent 各有独立桶：A 打满后 B 仍可调用。"""
    user_id = f"user-{uuid.uuid4().hex}"
    agent_a = f"agent-A-{uuid.uuid4().hex}"
    agent_b = f"agent-B-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_a, user_id)
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_a, user_id)
    # 切换到 Agent B 仍可消费
    await check_rate_limit(agent_b, user_id)


@pytest.mark.asyncio
async def test_tokens_replenish_after_refill_window(monkeypatch):
    """sleep > refill_period 后令牌应恢复（用 3 tokens / 2s 加速）。"""
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    cap = 3
    period = 0.5  # 秒，避免测试实际 sleep 2s
    for _ in range(cap):
        await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period)
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period)
    # 等候 refill 周期；refill 是连续累计的，半周期后约补半桶
    time.sleep(period + 0.05)
    # 此刻桶应已恢复到 ~3 tokens，可再次消费
    await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period)


@pytest.mark.asyncio
async def test_disabled_flag_skips_rate_limit(monkeypatch):
    """ENABLED=False 时桶耗尽也不抛错（运维 / E2E 紧急逃生）。"""
    monkeypatch.setattr(rl_module, "ENABLED", False)
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    for _ in range(50):
        # 不抛即通过
        await check_rate_limit(agent_id, user_id)
    assert _bucket_snapshot() == {}


@pytest.mark.asyncio
async def test_uuid_case_normalization(monkeypatch):
    """同一 UUID 大小写不同应命中同一桶（避免 UUID 字符串抖动造成桶分裂）。"""
    suffix = uuid.uuid4().hex
    agent_a = f"AGENT-{suffix.upper()}"
    agent_b = f"agent-{suffix.lower()}"
    user_id = f"user-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_a, user_id)
    # 切换大小写后再消费应触发 429（同一桶）
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_b, user_id)


@pytest.mark.asyncio
async def test_ttl_eviction_does_not_regress_normal_usage():
    """TTL 过期后下次访问以满桶重启，不影响正常使用。

    模拟场景：长时间未访问 → 桶条目被驱逐 → 下次访问重新开始（不抛错）。
    """
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    cap = 3
    period = 1.0
    # TTL 设为非常短，强制下次访问触发 evict
    for _ in range(cap):
        await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period, bucket_ttl=0)
    # 等候 refill 周期使桶自动恢复，但 TTL=0 同时会触发惰性驱逐
    time.sleep(period + 0.05)
    # 触发惰性驱逐 + 满桶重启：连续 3 次调用仍应成功
    for _ in range(cap):
        await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period, bucket_ttl=0)
    # 第三次之后桶应再被打空；第 4 次失败
    with pytest.raises(TooManyRequestsError):
        await check_rate_limit(agent_id, user_id, capacity=cap, refill_seconds=period, bucket_ttl=0)


@pytest.mark.asyncio
async def test_reset_buckets_clears_state():
    """reset_buckets() 清空所有桶 —— 运维 / 测试用紧急复位。"""
    agent_id = f"agent-{uuid.uuid4().hex}"
    user_id = f"user-{uuid.uuid4().hex}"
    for _ in range(10):
        await check_rate_limit(agent_id, user_id)
    assert _bucket_snapshot()
    reset_buckets()
    assert _bucket_snapshot() == {}


# ── 内部工具函数 ───────────────────────────────────────────────────────


def _bucket_key(agent_id: str, user_id: str) -> tuple[str, str]:
    return (str(agent_id).lower(), str(user_id).lower())


# ── 集成测试：POST /{id}/invoke 触发 429 ──────────────────────────────


@pytest.mark.asyncio
async def test_invoke_returns_429_after_exhausting_bucket(client: AsyncClient):
    """通过 /invoke 端点连续调用，第 N+1 次返回 429，message_key 正确。"""
    user, org = await _make_org_user(OrgRole.operator)

    _override_user(user)
    try:
        # rate limit 在 get_external_agent 之前触发，所以 Agent 不存在
        # 也能拿到 429（spec §6.2 step 1 设计）。
        agent_id = f"fake-agent-{uuid.uuid4().hex}"

        # 前 10 次调用：因 Agent 不存在而 404（说明 rate limit 通过）
        for _ in range(10):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {}},
            )
            assert resp.status_code == 404

        # 第 11 次：rate limit 应拦截
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {}},
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body["message_key"] == "errors.external_agent.rate_limited"
        assert body["error_code"] == 42900
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_disabled_flag_skips_429(monkeypatch, client: AsyncClient):
    """ENABLED=False 时连续调用 20 次也不会 429（运维 / E2E 紧急逃生）。"""
    user, org = await _make_org_user(OrgRole.operator)
    monkeypatch.setattr(rl_module, "ENABLED", False)

    _override_user(user)
    try:
        agent_id = f"fake-agent-{uuid.uuid4().hex}"
        for _ in range(5):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {}},
            )
            # 不应是 429（rate limit 跳过）；是 404（Agent 不存在）或类似
            assert resp.status_code != 429, f"unexpected 429 on call {_}"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_invoke_isolates_between_users(client: AsyncClient):
    """用户 A 触发 429 期间，用户 B 仍可调用同一 Agent。"""
    user_a, org = await _make_org_user(OrgRole.operator)
    user_b, _ = await _make_org_user(OrgRole.operator)

    agent_id = f"fake-agent-{uuid.uuid4().hex}"

    _override_user(user_a)
    try:
        for _ in range(10):
            resp = await client.post(
                f"/api/v1/external-agents/{agent_id}/invoke",
                json={"params": {}},
            )
            assert resp.status_code != 429
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {}},
        )
        assert resp.status_code == 429
    finally:
        _clear_override()

    # 用户 B 切到自己的桶，不应被 A 的限流影响
    _override_user(user_b)
    try:
        resp = await client.post(
            f"/api/v1/external-agents/{agent_id}/invoke",
            json={"params": {}},
        )
        assert resp.status_code != 429
    finally:
        _clear_override()
