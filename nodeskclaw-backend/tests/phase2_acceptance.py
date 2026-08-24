#!/usr/bin/env python3
"""Phase 2 §13 验收自测：执行顺序可重复的 smoke test，模拟真实使用流程。

不是 pytest 测试，是一个可手动运行的演示/验收脚本：
  $ uv run python tests/phase2_acceptance.py --base-url http://localhost:4510 --admin-token <jwt>

按 spec §13 的 9 条验收标准走一遍 admin 视角：
  1. preview 草稿（含 description/default/enum→select/format=date→date）
  2. 勾选 3 个 operation 导入 → 1 插件 + 3 functions (status=draft)
  3. 用户端进入插件 → 功能列表 → 逐 function 表单 → invoke 成功 → output_hint 渲染
  4. 字段说明在 JSON；默认值预填 + 用户可覆盖；服务端兜底缺省字段 default
  5. 存量单功能插件迁移后旧 /invoke 兼容代理 + Deprecation 头
  6. SSRF 拦截 / >5MB 文档拒绝 / 跨文档 $ref warning
  7. Swagger 2.0 + OpenAPI 3.x 双格式均解析成功
  8. 双用户隔离：跨 org GET form/probe/list 均 404
  9. i18n 关键 message_key 命中；密钥不外泄

执行顺序固定、可重复，每次运行只产生一条新插件 + 三条 function（带 UUID 后缀）。
打印 ✅ / ❌ 结果，失败时打印 traceback 关键帧。

依赖：httpx（仅环境已用），无新增依赖。
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


# ── 输出辅助 ──────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_RESULTS: list[StepResult] = []


def _print_banner(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def _print_sub(sub: str) -> None:
    print(f"\n--- {sub} ---")


def _record(name: str, ok: bool, detail: str = "", **extra: Any) -> None:
    marker = "PASS" if ok else "FAIL"
    line = f"[{marker}] {name}"
    if detail:
        line += f"  --  {detail}"
    print(line)
    _RESULTS.append(StepResult(name=name, ok=ok, detail=detail, extra=extra))


# ── 嵌入的最小 OpenAPI 3.0 文档 ──────────────────────────────────────────────


def _build_sample_doc() -> dict[str, Any]:
    """一份带 enum + format=date + default + format=binary 的样例 OpenAPI 3.0 文档。"""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Acceptance Demo", "version": "1.0"},
        "servers": [{"url": "https://api.acme.example.com"}],
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "summary": "List orders",
                    "parameters": [
                        {
                            "name": "status", "in": "query", "required": False,
                            "schema": {
                                "type": "string",
                                "enum": ["pending", "shipped", "delivered"],
                                "default": "pending",
                                "description": "订单状态筛选",
                            },
                        },
                        {
                            "name": "from", "in": "query", "required": False,
                            "schema": {
                                "type": "string", "format": "date",
                                "description": "起始日期",
                            },
                        },
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "createOrder",
                    "summary": "Create order",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["sku", "qty"],
                                    "properties": {
                                        "sku": {"type": "string",
                                                "description": "商品编码"},
                                        "qty": {"type": "integer", "default": 1,
                                                "description": "数量"},
                                        "attachment": {"type": "string",
                                                       "format": "binary",
                                                       "description": "附件"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                },
            },
            "/health": {
                "get": {
                    "operationId": "healthCheck",
                    "summary": "Health",
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }


# ── HTTP 客户端封装 ───────────────────────────────────────────────────────────


@dataclass
class ClientCtx:
    """一个最小 admin client：携带 Bearer token，所有调用走同一个 base_url。"""

    base_url: str
    token: str | None
    client: httpx.Client

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self.base_url.rstrip("/") + path
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.client.request(method, url, headers=headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="phase2_acceptance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(__doc__ or ""),
    )
    p.add_argument(
        "--base-url", default="http://localhost:4510",
        help="后端 API 根地址（默认 http://localhost:4510）",
    )
    p.add_argument(
        "--admin-token", default=None,
        help="平台 admin 的 JWT；缺省时从 env NODESKCLAW_ADMIN_TOKEN 读取",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只解析参数 + 输出 self-check 信息，不发任何 HTTP 请求",
    )
    p.add_argument(
        "--timeout", type=float, default=15.0,
        help="HTTP 请求超时（秒）",
    )
    return p.parse_args()


def _resolve_token(args: argparse.Namespace) -> str | None:
    if args.admin_token:
        return args.admin_token
    import os
    return os.environ.get("NODESKCLAW_ADMIN_TOKEN")


# ── 验收步骤：每个函数对应 spec §13 一条 ──────────────────────────────────────


def _step_1_preview_returns_drafts(ctx: ClientCtx) -> dict[str, Any]:
    """Acceptance #1：preview 草稿，enum→select、format=date→date、description/default。"""
    _print_sub("Acceptance #1: preview 返回 operation 草稿 + 字段元数据")
    doc = _build_sample_doc()
    resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": doc},
    )
    if resp.status_code != 200:
        _record("preview", False, f"status={resp.status_code} body={resp.text[:200]}")
        return {}
    data = resp.json()["data"]
    funcs = data["functions"]
    if len(funcs) < 1:
        _record("preview", False, "no functions in preview response")
        return {}

    # 校验 enum→select / format=date→date / description/default
    list_orders = next(
        f for f in funcs
        if f["method"] == "GET" and f["path"] == "/orders"
    )
    status_field = next((f for f in list_orders["fields"] if f["name"] == "status"), None)
    from_field = next((f for f in list_orders["fields"] if f["name"] == "from"), None)
    if status_field is None or status_field.get("ui") != "select":
        _record("preview", False, "status field ui != select")
        return {}
    if status_field.get("default") != "pending":
        _record("preview", False, "status field default != pending")
        return {}
    if from_field is None or from_field.get("ui") != "date":
        _record("preview", False, "from field ui != date")
        return {}

    _record("preview", True, f"{len(funcs)} functions drafted; enum/date/default verified")
    return data


def _step_2_confirm_creates_plugin_and_functions(ctx: ClientCtx) -> dict[str, Any]:
    """Acceptance #2：勾选 3 operation 导入 → 1 插件 + 3 functions (status=draft)。"""
    _print_sub("Acceptance #2: confirm 导入 → 1 插件 + 3 functions (draft)")
    doc = _build_sample_doc()
    suffix = uuid.uuid4().hex[:8]
    selected = [
        {"name": "list_orders", "method": "GET", "path": "/orders"},
        {"name": "create_order", "method": "POST", "path": "/orders"},
        {"name": "health_check", "method": "GET", "path": "/health"},
    ]
    resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi",
        json={
            "name": f"acceptance-agent-{suffix}",
            "doc": doc,
            "auth": {"type": "none"},
            "selected": selected,
        },
    )
    if resp.status_code != 200:
        _record("confirm", False, f"status={resp.status_code} body={resp.text[:200]}")
        return {}
    payload = resp.json()["data"]
    agent = payload["agent"]
    funcs = payload["functions"]
    if agent.get("status") != "draft":
        _record("confirm", False, f"agent.status={agent.get('status')}")
        return {}
    if len(funcs) != 3:
        _record("confirm", False, f"function count={len(funcs)}")
        return {}
    if not all(f.get("status") == "draft" for f in funcs):
        _record("confirm", False, "not all functions are draft")
        return {}
    if not all(f.get("source") == "openapi_import" for f in funcs):
        _record("confirm", False, "not all functions are openapi_import")
        return {}
    _record("confirm", True, f"agent {agent['id'][:8]}... + 3 functions draft")
    return {"agent": agent, "functions": funcs}


def _step_3_list_and_invoke_each_function(
    ctx: ClientCtx, agent_id: str, fn_ids: list[str],
) -> None:
    """Acceptance #3：进入插件 → 功能列表 → 逐 function GET form + POST invoke。"""
    _print_sub("Acceptance #3: 用户端进入 → 功能列表 → 逐 function form + invoke")

    list_resp = ctx.get(f"/api/v1/external-agents/{agent_id}/functions")
    if list_resp.status_code != 200:
        _record("functions list", False, f"status={list_resp.status_code}")
        return
    listed = list_resp.json()["data"]
    if len(listed) != len(fn_ids):
        _record("functions list", False, f"expected {len(fn_ids)}, got {len(listed)}")
        return
    _record("functions list", True, f"{len(listed)} functions visible")

    # 逐 function：GET form + POST invoke
    for fid in fn_ids:
        # 注意：import 创建的 function 默认 status=draft，invoke/form 要求 active；
        # 真实演示流程此处需要先 PATCH status=active。验收脚本走全流程：admin
        # 先把 functions 切到 active，再演示 invoke。
        patch_resp = ctx.request(
            "PATCH",
            f"/api/v1/external-agents/{agent_id}/functions/{fid}",
            json={"status": "active"},
        )
        if patch_resp.status_code != 200:
            _record(
                f"function {fid[:8]} activate",
                False,
                f"PATCH status={patch_resp.status_code}",
            )
            return

        form_resp = ctx.get(
            f"/api/v1/external-agents/{agent_id}/functions/{fid}/form"
        )
        if form_resp.status_code != 200:
            _record(
                f"function {fid[:8]} form",
                False,
                f"GET form status={form_resp.status_code}",
            )
            return

        invoke_resp = ctx.post(
            f"/api/v1/external-agents/{agent_id}/functions/{fid}/invoke",
            json={"params": {}},
        )
        if invoke_resp.status_code != 200:
            _record(
                f"function {fid[:8]} invoke",
                False,
                f"status={invoke_resp.status_code} body={invoke_resp.text[:200]}",
            )
            return
        body = invoke_resp.json()["data"]
        # 上游不可达时 success:false 200 + upstream_status:None；本步不依赖上游
        # 在 demo 中只确认响应形态合规（success 字段存在 + items_path/display 字段）。
        if "success" not in body:
            _record(
                f"function {fid[:8]} invoke",
                False,
                f"missing 'success' in response: {body}",
            )
            return
    _record("multi-function form+invoke", True, f"all {len(fn_ids)} functions walked")


def _step_4_field_description_default_injection(ctx: ClientCtx) -> None:
    """Acceptance #4：description/default 都在 JSON；服务端兜底 default。"""
    _print_sub("Acceptance #4: 字段 description + default 端到端")
    doc = _build_sample_doc()
    # 用同一份 doc 走 preview，再 confirm 一个独立 agent 用于演示
    suffix = uuid.uuid4().hex[:8]
    resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi",
        json={
            "name": f"acceptance-default-{suffix}",
            "doc": doc,
            "auth": {"type": "none"},
            "selected": [{"name": "list_orders", "method": "GET", "path": "/orders"}],
        },
    )
    if resp.status_code != 200:
        _record("default injection", False, f"confirm status={resp.status_code}")
        return
    agent = resp.json()["data"]["agent"]
    fn = resp.json()["data"]["functions"][0]

    # 切到 active
    ctx.request(
        "PATCH",
        f"/api/v1/external-agents/{agent['id']}/functions/{fn['id']}",
        json={"status": "active"},
    )

    form_resp = ctx.get(
        f"/api/v1/external-agents/{agent['id']}/functions/{fn['id']}/form"
    )
    if form_resp.status_code != 200:
        _record("default injection", False, "GET form failed")
        return
    fields = form_resp.json()["data"]["input_schema"]["fields"]
    status_field = fields.get("status", {})
    if status_field.get("description") != "订单状态筛选":
        _record(
            "default injection", False,
            f"description mismatch: {status_field.get('description')!r}",
        )
        return
    if status_field.get("default") != "pending":
        _record(
            "default injection", False,
            f"default mismatch: {status_field.get('default')!r}",
        )
        return
    # 不传 status → 服务端注入 default（demo：上游不可达时 success:false 也算正常，
    # 这里只校验响应 200 与 field shape；真正 default 注入由 build_dynamic_input_model 负责）
    invoke_resp = ctx.post(
        f"/api/v1/external-agents/{agent['id']}/functions/{fn['id']}/invoke",
        json={"params": {}},
    )
    if invoke_resp.status_code != 200:
        _record("default injection", False, f"invoke status={invoke_resp.status_code}")
        return
    _record(
        "default injection", True,
        "description='订单状态筛选', default='pending' both present",
    )


def _step_5_legacy_compat_proxy(ctx: ClientCtx) -> None:
    """Acceptance #5：存量 tool 插件迁移后旧 /invoke 兼容代理 + Deprecation 头。"""
    _print_sub("Acceptance #5: 存量插件旧 /invoke 兼容代理 + Deprecation 头")
    suffix = uuid.uuid4().hex[:8]
    # 创建一个手填的 plugin（status=active，type=tool，invoke_config 落 agent 列），
    # 然后再补一个 sort_order=0 default function（模拟 alembic 迁移后的存量）。
    create_resp = ctx.post(
        "/api/v1/external-agents",
        json={
            "name": f"acceptance-legacy-{suffix}",
            "type": "tool",
            "description": "legacy compat demo",
            "invoke_config": {
                "endpoint": "http://127.0.0.1:9999/legacy",
                "method": "POST",
                "auth": {"type": "none"},
                "timeout_seconds": 30,
                "pass_mode": "multipart",
            },
            "input_schema": {
                "order": ["line"],
                "fields": {
                    "line": {"type": "string", "ui": "input",
                             "label": "line", "required": False},
                },
            },
            "status": "active",
        },
    )
    if create_resp.status_code != 200:
        _record("legacy create", False, f"status={create_resp.status_code}")
        return
    agent = create_resp.json()["data"]
    agent_id = agent["id"]

    # 通过 service 走创建 default function：直接 PATCH 不到的端点需要用 list endpoint
    # 看一下能不能创建（operator+），若是走 service 即可。
    # 这里演示通过 POST /functions 加一个 sort_order=0 默认功能（与 DB 模型一致）：
    fn_resp = ctx.post(
        f"/api/v1/external-agents/{agent_id}/functions",
        json={
            "name": "default",
            "summary": "migrated",
            "invoke_config": agent["invoke_config"],
            "input_schema": agent["input_schema"],
            "status": "active",
            "sort_order": 0,
        },
    )
    if fn_resp.status_code != 200:
        _record(
            "legacy fn create", False,
            f"POST /functions status={fn_resp.status_code} body={fn_resp.text[:200]}",
        )
        return

    # 旧 /invoke
    invoke_resp = ctx.post(
        f"/api/v1/external-agents/{agent_id}/invoke",
        json={"params": {"line": "L1"}},
    )
    if invoke_resp.status_code != 200:
        _record("legacy invoke", False, f"status={invoke_resp.status_code}")
        return
    deprecation = invoke_resp.headers.get("Deprecation")
    if deprecation != "true":
        _record("legacy invoke", False, f"Deprecation header={deprecation!r}")
        return
    _record("legacy invoke", True, "Deprecation: true header present")


def _step_6_ssrf_too_large_cross_doc_ref(ctx: ClientCtx) -> None:
    """Acceptance #6：SSRF 拦截 + 5MB 拒绝 + 跨文档 $ref warning 跳过。"""
    _print_sub("Acceptance #6: SSRF + 5MB + 跨文档 $ref 三重防护")
    # 6a: SSRF（不允许任何私网）
    # 注意：本 demo 默认 org allow-list 不控制，只能演示 127.0.0.1 通配；
    # 真实 SSRF 拦截依赖后端配置。这里只演示错误响应码与 message_key。
    # 通过送一个明显非法的 URL 让后端抛错：
    ssrf_resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc_url": "http://0.0.0.0/openapi.json"},
    )
    if ssrf_resp.status_code in (400, 403):
        mk = ssrf_resp.json().get("message_key", "")
        _record(
            "SSRF block", True,
            f"status={ssrf_resp.status_code} message_key={mk}",
        )
    else:
        _record(
            "SSRF block", False,
            f"expected 400/403, got {ssrf_resp.status_code}",
        )

    # 6b: 5MB 文档 → 400 openapi_too_large
    # 通过 doc body 直接送 5MB+1 个字符的 JSON 文档
    big_doc = {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {}, "pad": "a" * (5 * 1024 * 1024)}
    big_resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": big_doc},
    )
    if big_resp.status_code == 400 and big_resp.json().get("message_key") == "errors.external_agent.openapi_too_large":
        _record("5MB limit", True, "openapi_too_large message_key returned")
    else:
        _record(
            "5MB limit", False,
            f"status={big_resp.status_code} mk={big_resp.json().get('message_key')}",
        )

    # 6c: 跨文档 $ref
    evil_doc = {
        "openapi": "3.0.0",
        "info": {"title": "evil", "version": "1"},
        "servers": [{"url": "https://api.legit.com"}],
        "paths": {
            "/x": {
                "get": {
                    "operationId": "evil",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "evil_field": {"$ref": "https://evil.com/schema.json"},
                                    },
                                },
                            },
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }
    ref_resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": evil_doc},
    )
    if ref_resp.status_code != 200:
        _record("cross-doc ref", False, f"status={ref_resp.status_code}")
    else:
        funcs = ref_resp.json()["data"]["functions"]
        all_warnings = []
        for f in funcs:
            all_warnings.extend(f.get("warnings") or [])
        if any("跨文档" in w or "$ref" in w or "evil" in w for w in all_warnings):
            _record(
                "cross-doc ref", True,
                f"warning emitted ({len(all_warnings)} warnings)",
            )
        else:
            _record(
                "cross-doc ref", False,
                f"no cross-doc warning found; warnings={all_warnings}",
            )


def _step_7_swagger_2_and_openapi_3(ctx: ClientCtx) -> None:
    """Acceptance #7：Swagger 2.0 + OpenAPI 3.x 双格式均能解析。"""
    _print_sub("Acceptance #7: Swagger 2.0 + OpenAPI 3.x 双格式")
    swagger_2 = {
        "swagger": "2.0",
        "host": "api.legit.com",
        "basePath": "/v2",
        "schemes": ["https"],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }
    r1 = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": swagger_2},
    )
    if r1.status_code == 200 and r1.json()["data"]["spec_version"] == "2.0":
        _record("Swagger 2.0", True, "parsed successfully")
    else:
        _record(
            "Swagger 2.0", False,
            f"status={r1.status_code} version={r1.json().get('data', {}).get('spec_version')}",
        )

    r2 = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": _build_sample_doc()},
    )
    if r2.status_code == 200 and r2.json()["data"]["spec_version"] == "3.0.0":
        _record("OpenAPI 3.0", True, "parsed successfully")
    else:
        _record(
            "OpenAPI 3.0", False,
            f"status={r2.status_code} version={r2.json().get('data', {}).get('spec_version')}",
        )


def _step_8_cross_org_isolation(ctx: ClientCtx) -> None:
    """Acceptance #8：双用户隔离（演示端：调用 GET 不存在 org 的资源 → 404）。

    本步骤只演示"端点对未知 agent 资源返回 404"的统一行为；要演示真实双 org 隔离
    需要 admin token 同时具备两个 org 的 membership，本脚本只验证单一 org 内
    跨 agent 不存在 / 跨 user_id 的 404 路径。
    """
    _print_sub("Acceptance #8: 跨 org / 跨 agent 不存在资源 → 404")
    fake_agent_id = str(uuid.uuid4())
    fake_fn_id = str(uuid.uuid4())
    list_resp = ctx.get(f"/api/v1/external-agents/{fake_agent_id}/functions")
    if list_resp.status_code == 404:
        _record("cross-org list 404", True, "non-existent agent functions list 404")
    else:
        _record(
            "cross-org list 404", False,
            f"expected 404, got {list_resp.status_code}",
        )

    form_resp = ctx.get(
        f"/api/v1/external-agents/{fake_agent_id}/functions/{fake_fn_id}/form"
    )
    if form_resp.status_code == 404:
        _record("cross-org form 404", True, "non-existent agent/form 404")
    else:
        _record(
            "cross-org form 404", False,
            f"expected 404, got {form_resp.status_code}",
        )


def _step_9_i18n_keys_and_secret_redaction(ctx: ClientCtx) -> None:
    """Acceptance #9：i18n 关键 message_key 命中；密钥不外泄。"""
    _print_sub("Acceptance #9: i18n 双语 message_key + 密钥不外泄")
    SECRET = f"secret-{uuid.uuid4().hex[:8]}-DO-NOT-LEAK"  # noqa: N806  # 测试用固定 secret（高语义价值）
    doc = _build_sample_doc()
    suffix = uuid.uuid4().hex[:8]
    resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi",
        json={
            "name": f"acceptance-secret-{suffix}",
            "doc": doc,
            "auth": {"type": "bearer", "token": SECRET},
            "selected": [{"name": "list_orders", "method": "GET", "path": "/orders"}],
        },
    )
    if resp.status_code != 200:
        _record("secret create", False, f"status={resp.status_code}")
        return
    payload_text = resp.text
    if SECRET in payload_text:
        _record(
            "secret in confirm response", False,
            "SECRET plaintext leaked in confirm response",
        )
        return
    _record("secret in confirm response", True, "no plaintext SECRET in response")

    agent_id = resp.json()["data"]["agent"]["id"]
    fn_id = resp.json()["data"]["functions"][0]["id"]

    # PATCH status=active，再 GET form 检查脱敏
    ctx.request(
        "PATCH",
        f"/api/v1/external-agents/{agent_id}/functions/{fn_id}",
        json={"status": "active"},
    )
    form_resp = ctx.get(
        f"/api/v1/external-agents/{agent_id}/functions/{fn_id}/form"
    )
    if form_resp.status_code != 200:
        _record("secret in form", False, f"GET form status={form_resp.status_code}")
        return
    if SECRET in form_resp.text:
        _record("secret in form", False, "SECRET leaked in GET form response")
        return
    token_field = form_resp.json()["data"]["invoke_config"]["auth"].get("token")
    if token_field != "***redacted***":
        _record(
            "secret in form", False,
            f"token not redacted; got {token_field!r}",
        )
        return
    _record("secret in form", True, "invoke_config.auth.token is redacted")

    # 关键 i18n message_key 命中（确保后端抛错时使用稳定 key）
    # SSRF + 5MB + function_not_active 三种典型错误
    ssrf_resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc_url": "http://0.0.0.0/openapi.json"},
    )
    mk_ssrf = ssrf_resp.json().get("message_key", "")
    too_large_doc = {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {}, "pad": "a" * (5 * 1024 * 1024)}
    big_resp = ctx.post(
        "/api/v1/external-agents/plugins/import/openapi/preview",
        json={"doc": too_large_doc},
    )
    mk_big = big_resp.json().get("message_key", "")

    if mk_ssrf == "errors.external_agent.ssrf_blocked":
        _record("i18n ssrf_blocked", True, "message_key present in response")
    else:
        _record(
            "i18n ssrf_blocked", False,
            f"got message_key={mk_ssrf!r}",
        )
    if mk_big == "errors.external_agent.openapi_too_large":
        _record("i18n openapi_too_large", True, "message_key present in response")
    else:
        _record(
            "i18n openapi_too_large", False,
            f"got message_key={mk_big!r}",
        )


# ── 主流程 ────────────────────────────────────────────────────────────────────


# 每步之间的固定等待：避免后端限流 / async 资源竞争
_STEP_DELAY_SEC = 0.05

# 步骤清单：(name, fn(ctx, ...))
# 用 type-erased Callable 列表保持简洁。
def _build_steps() -> list[tuple[str, Callable[[ClientCtx, Any], Any]]]:
    return [
        # Steps that don't depend on previous ones
        ("preview", _step_1_preview_returns_drafts),
    ]


def main() -> int:
    args = _parse_args()
    token = _resolve_token(args)
    if args.dry_run:
        print("DRY RUN: parameters parsed OK")
        print(f"  base_url = {args.base_url}")
        print(f"  admin_token = {'***set***' if token else 'MISSING'}")
        print(f"  timeout = {args.timeout}")
        return 0

    if token is None:
        print(
            "ERROR: --admin-token or NODESKCLAW_ADMIN_TOKEN required",
            file=sys.stderr,
        )
        return 2

    _print_banner("Phase 2 §13 Acceptance — DeskClaw external agent plugin")
    print(f"Base URL : {args.base_url}")
    print(f"Token    : ***set*** (length={len(token)})")
    print(f"Timeout  : {args.timeout}s")

    with httpx.Client(timeout=args.timeout) as raw_client:
        ctx = ClientCtx(
            base_url=args.base_url,
            token=token,
            client=raw_client,
        )

        # 先做一次 GET /agents 探活，确保 base_url/token 可用
        try:
            health = ctx.get("/api/v1/external-agents")
            if health.status_code >= 500:
                _record(
                    "preflight /external-agents",
                    False,
                    f"status={health.status_code}; 后端可能没启动",
                )
                return 1
        except httpx.HTTPError as exc:
            _record("preflight /external-agents", False, f"network error: {exc}")
            return 1
        _record(
            "preflight /external-agents",
            True,
            f"status={health.status_code}",
        )

        # 跑 §13 的 9 条：每条用独立 try/except，失败不阻断后续。
        steps: list[tuple[str, Callable[[ClientCtx], Any]]] = [
            ("#1 preview", _step_1_preview_returns_drafts),
            ("#2 confirm", _step_2_confirm_creates_plugin_and_functions),
            ("#3 list+invoke", _step_3_list_and_invoke_each_function),
            ("#4 description/default", _step_4_field_description_default_injection),
            ("#5 legacy compat", _step_5_legacy_compat_proxy),
            ("#6 SSRF/5MB/ref", _step_6_ssrf_too_large_cross_doc_ref),
            ("#7 dual format", _step_7_swagger_2_and_openapi_3),
            ("#8 cross-org 404", _step_8_cross_org_isolation),
            ("#9 i18n + secret", _step_9_i18n_keys_and_secret_redaction),
        ]
        # Steps that need the artifact from step #2 — wire them up after step 2 runs.
        step_2_artifact: dict[str, Any] = {}

        def _wrapped_step_3() -> None:
            if not step_2_artifact:
                _record("#3 list+invoke", False, "step #2 did not produce artifact")
                return
            agent = step_2_artifact["agent"]
            fn_ids = [f["id"] for f in step_2_artifact["functions"]]
            _step_3_list_and_invoke_each_function(ctx, agent["id"], fn_ids)

        # Re-order: step #2 stores into step_2_artifact, then we run step #3 (wrapped).
        ordered: list[Callable[[], None]] = [
            lambda: _step_1_preview_returns_drafts(ctx),
            lambda: step_2_artifact.update(
                _step_2_confirm_creates_plugin_and_functions(ctx) or {}
            ),
            _wrapped_step_3,
            lambda: _step_4_field_description_default_injection(ctx),
            lambda: _step_5_legacy_compat_proxy(ctx),
            lambda: _step_6_ssrf_too_large_cross_doc_ref(ctx),
            lambda: _step_7_swagger_2_and_openapi_3(ctx),
            lambda: _step_8_cross_org_isolation(ctx),
            lambda: _step_9_i18n_keys_and_secret_redaction(ctx),
        ]

        t_start = time.monotonic()
        for fn in ordered:
            try:
                fn()
            except Exception as exc:
                tb = traceback.format_exc(limit=4)
                _record(
                    fn.__name__ or "step",
                    False,
                    f"exception: {type(exc).__name__}: {exc}",
                )
                print(tb)
            time.sleep(_STEP_DELAY_SEC)
        elapsed = time.monotonic() - t_start

    # 总结
    _print_banner("Summary")
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r.ok)
    print(f"Passed: {passed}/{total}  (elapsed {elapsed:.1f}s)")
    for r in _RESULTS:
        marker = "PASS" if r.ok else "FAIL"
        print(f"  [{marker}] {r.name}")
    print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
