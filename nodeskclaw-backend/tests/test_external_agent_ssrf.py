"""SSRF 防护单测（Phase 1 任务 #5 / spec §9.2）。

覆盖：
  - _resolve_wsl_endpoint 在 DEBUG=false 时是 no-op
  - _resolve_wsl_endpoint 在 DEBUG=true 时仍会重写（保留原有 WSL 开发体验）
  - is_private_or_loopback 对各类 RFC1918 / loopback / link-local / IPv6 的判定
  - check_endpoint_allowed：公网放行、私网需在 allow-list 中才放行
  - 硬禁：169.254.169.254、0.0.0.0 即便在 allow-list 中也拒绝
  - validate_invoke_endpoint 抛 ForbiddenError + 正确的 i18n message_key
  - HTTP 端到端：POST /plugins/validate 在 SSRF-blocked 时不发起外网请求
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services.external_agent_adapter import (
    _resolve_wsl_endpoint,
)
from app.services.external_agent_ssrf import (
    check_endpoint_allowed,
    is_private_or_loopback,
    validate_invoke_endpoint,
)
from tests.conftest import TestSessionLocal


# ── _resolve_wsl_endpoint DEBUG gate ─────────────────────────────────────────


def test_resolve_wsl_endpoint_noop_when_debug_false(monkeypatch):
    """DEBUG=false 时永远不动 URL —— 生产安全要求。"""
    monkeypatch.setattr(settings, "DEBUG", False, raising=False)
    out = _resolve_wsl_endpoint("http://127.0.0.1:8000/api")
    assert out == "http://127.0.0.1:8000/api"
    out = _resolve_wsl_endpoint("http://localhost:8080/health")
    assert out == "http://localhost:8080/health"
    out = _resolve_wsl_endpoint("http://10.50.54.233:4080/api")
    assert out == "http://10.50.54.233:4080/api"


def test_resolve_wsl_endpoint_preserves_existing_debug_behavior(monkeypatch):
    """DEBUG=true 时仍走原 WSL 逻辑（非 WSL 环境下检测会 early return；
    本测试只验证函数不会因为新加 DEBUG gate 而抛错或重写非 127.0.0.1 地址）。"""
    monkeypatch.setattr(settings, "DEBUG", True, raising=False)
    # 非 loopback 永远不变
    out = _resolve_wsl_endpoint("http://10.50.54.233:4080/api")
    assert out == "http://10.50.54.233:4080/api"
    out = _resolve_wsl_endpoint("https://example.com/foo")
    assert out == "https://example.com/foo"


# ── is_private_or_loopback ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.0.1",
        "192.168.255.254",
        "169.254.169.254",
        "169.254.0.1",
        "::1",
        "fc00::1",
        "fd00::1",
        "fe80::1",
        "0.0.0.0",  # 通配：也是 link/global 边界，但 ipaddress.is_private 不一定返回 True
    ],
)
def test_is_private_or_loopback_returns_true(host):
    assert is_private_or_loopback(host) is True, host


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_is_private_or_loopback_returns_false_for_public(host):
    assert is_private_or_loopback(host) is False, host


def test_is_private_or_loopback_for_ipv4_mapped_ipv6():
    """::ffff:10.0.0.1 这类 IPv4-mapped IPv6 也应当作私网拦截。"""
    assert is_private_or_loopback("::ffff:10.0.0.1") is True


def test_is_private_or_loopback_for_hostname_returns_false():
    """域名形态（无法静态判定）放行——实际 DNS 解析时的拦截由 SSRF guard 的网络层处理。"""
    assert is_private_or_loopback("agent.internal.example.com") is False


# ── check_endpoint_allowed ──────────────────────────────────────────────────


def test_check_endpoint_allowed_public_ok():
    ok, err = check_endpoint_allowed("https://agent.example.com:8443/v1", [])
    assert ok is True
    assert err is None


def test_check_endpoint_allowed_private_without_allowlist_blocked():
    ok, err = check_endpoint_allowed("http://10.50.54.233:4080/api", [])
    assert ok is False
    assert err is not None


def test_check_endpoint_allowed_private_with_matching_cidr_ok():
    ok, err = check_endpoint_allowed(
        "http://10.50.54.233:4080/api",
        ["10.0.0.0/8", "192.168.0.0/16"],
    )
    assert ok is True
    assert err is None


def test_check_endpoint_allowed_loopback_blocked_without_allowlist():
    ok, err = check_endpoint_allowed("http://127.0.0.1:8000/x", [])
    assert ok is False
    assert err is not None


def test_check_endpoint_allowed_loopback_with_explicit_cidr_ok():
    ok, err = check_endpoint_allowed("http://127.0.0.1:8000/x", ["127.0.0.0/8"])
    assert ok is True


def test_check_endpoint_allowed_169_254_169_254_hard_blocked_even_with_cidr():
    """云元数据 169.254.169.254 即便在 allow-list 里也必须被拒。"""
    ok, err = check_endpoint_allowed(
        "http://169.254.169.254/latest/meta-data/",
        ["169.254.0.0/16"],
    )
    assert ok is False
    assert "169.254.169.254" in (err or "")


def test_check_endpoint_allowed_0_0_0_0_hard_blocked():
    """0.0.0.0 是通配绑定，对外访问语义无意义，永久拒。"""
    ok, err = check_endpoint_allowed("http://0.0.0.0:8080/", ["0.0.0.0/0"])
    assert ok is False


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://example.com/foo",
        "",
        "javascript:alert(1)",
    ],
)
def test_check_endpoint_allowed_invalid_url_rejected(bad_url):
    ok, err = check_endpoint_allowed(bad_url, [])
    assert ok is False
    assert err == "目标地址格式无效"


def test_validate_invoke_endpoint_raises_forbidden_for_blocked():
    """validate_invoke_endpoint 是 SSRF 闸门：blocked 时直接 ForbiddenError。"""
    with pytest.raises(ForbiddenError) as ei:
        validate_invoke_endpoint("http://10.0.0.1:8000/", [])
    assert ei.value.message_key == "errors.external_agent.ssrf_blocked"


def test_validate_invoke_endpoint_raises_for_invalid_url():
    with pytest.raises(ForbiddenError) as ei:
        validate_invoke_endpoint("not-a-url", [])
    assert ei.value.message_key == "errors.external_agent.ssrf_invalid_url"


def test_validate_invoke_endpoint_passes_for_public():
    validate_invoke_endpoint("https://example.com/agent", [])  # 不抛


def test_validate_invoke_endpoint_passes_for_allowed_private():
    validate_invoke_endpoint("http://10.0.0.1:8000/", ["10.0.0.0/8"])  # 不抛


# ── HTTP 端到端：POST /plugins/validate 在 SSRF-blocked 时不发起外网请求 ──


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(role: str) -> tuple[User, Organization]:
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"ssrf-org-{suffix}",
            slug=f"ssrf-org-{suffix}",
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"ssrf-{suffix}@example.com", name=f"ssrf-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        await db.refresh(org)
        return user, org


@pytest.mark.asyncio
async def test_validate_blocks_private_endpoint_without_firing_probe(
    client: AsyncClient, monkeypatch
):
    """私网 endpoint + org 没配 allow-list 时，validate 必须返回 schema_ok=false，
    且不能触发任何外网请求（probe 路径上的 httpx 调用次数应为 0）。

    用 monkeypatch 替换 probe_chat_endpoint / probe_tool_invoke 来检测是否被调用过。
    """
    user, _ = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        called = {"chat": 0, "tool": 0}

        async def _fake_chat_probe(**kwargs):
            called["chat"] += 1
            return {"ok": False, "http_code": None, "latency_ms": 0, "error": "should not be called"}

        async def _fake_tool_probe(invoke, **kwargs):
            called["tool"] += 1
            return {"ok": False, "http_code": None, "latency_ms": 0, "error": "should not be called",
                    "skipped_invoke": True}

        from app.services import external_agent_adapter
        monkeypatch.setattr(
            external_agent_adapter, "probe_chat_endpoint", _fake_chat_probe
        )
        monkeypatch.setattr(
            external_agent_adapter, "probe_tool_invoke", _fake_tool_probe
        )

        # chat 型私网：未配 allow-list，应被 SSRF 拦在 schema 层
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "x", "type": "chat", "protocol": "rag_standard",
            "endpoint": "http://10.50.54.233:4080/api",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is False
        assert any("私网" in e or "白名单" in e for e in (data.get("schema_errors") or []))
        assert data["connectivity"] is None
        assert called["chat"] == 0, "probe_chat_endpoint 不应在 SSRF-blocked 时被触发"
        assert called["tool"] == 0

        # tool 型私网 invoke.endpoint：同样应被 SSRF 拦截
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "y", "type": "tool",
            "invoke": {"endpoint": "http://192.168.0.1:9000/api", "method": "POST"},
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is False
        assert any("私网" in e or "白名单" in e for e in (data.get("schema_errors") or []))
        assert called["tool"] == 0, "probe_tool_invoke 不应在 SSRF-blocked 时被触发"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_validate_blocks_cloud_metadata_even_with_allowlist(
    client: AsyncClient, monkeypatch
):
    """169.254.169.254 即便 org allow-list 显式允许 169.254.0.0/16，也必须被拒。"""
    user, org = await _make_org_user(OrgRole.operator)
    # 让 org 显式带上 169.254.0.0/16 —— 验证硬禁生效
    async with TestSessionLocal() as db:
        org_row = await db.get(Organization, org.id)
        org_row.external_agent_allowed_cidrs = ["169.254.0.0/16"]
        await db.commit()
    _override_user(user)
    try:
        called = {"chat": 0}

        async def _fake_chat_probe(**kwargs):
            called["chat"] += 1
            return {"ok": False, "http_code": None, "latency_ms": 0, "error": "should not"}

        from app.services import external_agent_adapter
        monkeypatch.setattr(
            external_agent_adapter, "probe_chat_endpoint", _fake_chat_probe
        )

        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "evil", "type": "chat", "protocol": "rag_standard",
            "endpoint": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is False
        assert any("169.254.169.254" in e for e in (data.get("schema_errors") or []))
        assert called["chat"] == 0
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_validate_allows_private_when_cidr_matches(
    client: AsyncClient, monkeypatch
):
    """私网 endpoint 在 org 显式允许 10.0.0.0/8 时应能通过 SSRF 检查并触发连通性试调。"""
    user, org = await _make_org_user(OrgRole.operator)
    async with TestSessionLocal() as db:
        org_row = await db.get(Organization, org.id)
        org_row.external_agent_allowed_cidrs = ["10.0.0.0/8"]
        await db.commit()
    _override_user(user)
    try:
        called = {"chat": 0}

        async def _fake_chat_probe(**kwargs):
            called["chat"] += 1
            # endpoint 已被 SSRF guard 放过（10.0.0.0/8 命中），传入 kwargs 的 endpoint
            # 与原值一致 —— 这里只返回失败连通，验证 probe 真的被调用了
            assert kwargs.get("allowed_cidrs") == ["10.0.0.0/8"]
            return {"ok": False, "http_code": None, "latency_ms": 0, "error": "unreachable"}

        from app.services import external_agent_adapter
        monkeypatch.setattr(
            external_agent_adapter, "probe_chat_endpoint", _fake_chat_probe
        )

        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "internal-rag", "type": "chat", "protocol": "rag_standard",
            "endpoint": "http://10.50.54.233:4080/api",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is True
        assert called["chat"] == 1, "SSRF 闸门放行后连通性试调应被触发"
    finally:
        _clear_override()
