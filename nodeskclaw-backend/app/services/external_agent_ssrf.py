"""外部智能体插件出站请求的 SSRF 防护（Phase 1 任务 #5 / 方案 §9.2）。

威胁模型：管理员提交的 URL（普通用户请求本就全程由后端代理发起）。
防护点：
  1. 在提交 / 更新 / 试调 / 代理调用前，先做 SSRF 检查；
  2. 默认拦截所有私网 / 回环 / 链路本地 / 0.0.0.0；
  3. org 级别可显式 allow-list 私网 CIDR（如内网 10.x）；
  4. 硬禁云元数据地址 169.254.169.254（即便在 allow-list 也拒绝）；
  5. 与 _resolve_wsl_endpoint 的 DEBUG-门控正交：本模块在 DEBUG=false 时同样生效，
     避免生产误开 localhost 重写时把"已被 SSRF 拦截"的私网地址绕过检测。

实现要点：
  - 纯函数 + stdlib（`ipaddress`），便于单元测试；
  - 错误响应走 i18n message_key `errors.external_agent.ssrf_blocked` /
    `errors.external_agent.ssrf_invalid_url`；
  - 仅在调用 `validate_invoke_endpoint` 时落 ForbiddenError，
    `check_endpoint_allowed` / `is_private_or_loopback` 留给上层做软提示（如向导 warning）。
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# 硬拦截地址：云元数据 + 通配绑定（0.0.0.0 / ::）。
# 即便 org 把 169.254.0.0/16 加入了 allow-list，这两类地址永远拒；
# 防止"允许私网"被滥用成"绕过 IMDS / 探活内网监听所有接口的服务"。
_HARD_BLOCKED_HOSTS: frozenset[str] = frozenset({
    "169.254.169.254",  # AWS / 主流云的实例元数据服务
    "0.0.0.0",          # 通配绑定，对外访问语义无意义
    "::",               # IPv6 通配
})


def is_private_or_loopback(host: str) -> bool:
    """判定 host 是否落在私网 / 回环 / 链路本地范围内。

    范围：127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
          169.254.0.0/16 (含云元数据), ::1, fc00::/7, fe80::/10。
    借助 stdlib `ipaddress.is_private` / `is_loopback` / `is_link_local` 判定。
    """
    if not host:
        return False
    # 域名形态直接放行（DNS 解析在 httpx 真正发起请求时发生）
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) 也需拦截 — ipaddress 不总把 mapped 视作 private
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        mapped = ip.ipv4_mapped
        if mapped.is_loopback or mapped.is_private or mapped.is_link_local:
            return True
    return False


def _parse_endpoint(endpoint: str) -> tuple[str, str] | None:
    """解析 endpoint，返回 (scheme, host) 元组；解析失败返回 None。"""
    if not endpoint:
        return None
    try:
        parsed = urlparse(endpoint)
    except Exception:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None
    return scheme, host


def _host_in_cidrs(host: str, cidrs: list[str]) -> bool:
    """判断 host 是否落在任一 CIDR 范围内；CIDR 列表允许 IPv4 / IPv6 混用。"""
    if not cidrs:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for raw in cidrs:
        cidr = (raw or "").strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if ip.version != network.version:
            continue
        if ip in network:
            return True
    return False


def check_endpoint_allowed(
    endpoint: str,
    allowed_cidrs: list[str] | None,
) -> tuple[bool, str | None]:
    """校验 endpoint 是否允许出站访问。

    返回 (ok, error_message_or_None)：
      - ok=True 表示允许放行；
      - ok=False 时 error_message 为人类可读的中文错误描述，前端可直接展示给管理员。

    规则：
      1. URL 必须是合法 http(s) 形式，否则拒绝；
      2. host 命中 _HARD_BLOCKED_HOSTS 直接拒绝；
      3. host 是私网 / 回环 / 链路本地：必须在 allowed_cidrs 中找到匹配 CIDR 才放行；
      4. 公网 host 直接放行。
    """
    parsed = _parse_endpoint(endpoint)
    if parsed is None:
        return False, "目标地址格式无效"

    _, host = parsed

    if host in _HARD_BLOCKED_HOSTS:
        return False, f"目标地址 {host} 被硬性禁止（云元数据或通配绑定）"

    if is_private_or_loopback(host):
        if _host_in_cidrs(host, allowed_cidrs or []):
            return True, None
        return False, "目标地址属于私网/回环地址，需要组织管理员加入允许的网段白名单"

    return True, None


def validate_invoke_endpoint(endpoint: str, allowed_cidrs: list[str] | None) -> None:
    """出站 HTTP 前的 SSRF 闸门：blocked 时直接抛 ForbiddenError。

    所有外部插件 outbound 调用前必须先调用此函数；它把硬禁 + 私网 + allow-list
    三层规则汇总成一个 i18n 化的业务错误，便于 API 层统一转 403 响应。
    """
    from app.core.exceptions import ForbiddenError  # 延迟 import：避免循环依赖

    ok, err = check_endpoint_allowed(endpoint, allowed_cidrs)
    if ok:
        return
    if err == "目标地址格式无效":
        raise ForbiddenError(
            message="目标地址格式无效",
            message_key="errors.external_agent.ssrf_invalid_url",
        )
    logger.warning(
        "SSRF blocked: endpoint=%s allowed_cidrs=%s reason=%s",
        endpoint, allowed_cidrs, err,
    )
    raise ForbiddenError(
        message="目标地址被安全策略拦截",
        message_key="errors.external_agent.ssrf_blocked",
    )
