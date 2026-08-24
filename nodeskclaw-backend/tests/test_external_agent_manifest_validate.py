"""验证插件 Manifest Schema 校验规则 + POST /plugins/validate 端点。

覆盖范围：外部智能体插件化接入 Phase 1 §4（Manifest 字段规范）+ §6.1（/plugins/validate）。
见 ee/docs/外部智能体一期方案.md 任务 #2。
"""
import uuid

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.schemas.external_agent import (
    ManifestInputSchema,
    PluginManifest,
    build_dynamic_input_model,
)
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_user(role: str):
    """预置组织 SSRF 白名单为内网段；本文件测试用 127.0.0.1 探活路径。

    SSRF 防护行为本身在 test_external_agent_ssrf.py 中覆盖。
    """
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"manifest-org-{suffix}", slug=f"manifest-org-{suffix}",
            external_agent_allowed_cidrs=["10.0.0.0/8", "127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        user = User(
            email=f"manifest-{suffix}@example.com", name=f"manifest-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user


# ── 纯 Pydantic 单元测试：Manifest Schema 规则（§4）─────────────────────────────

def test_chat_manifest_requires_protocol_and_endpoint():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"name": "x", "type": "chat"})


def test_chat_manifest_session_managed_by_external_requires_rag_standard():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "chat", "protocol": "openai_compatible",
            "endpoint": "http://example.com", "session_managed_by": "external",
        })

    # rag_standard 允许
    manifest = PluginManifest.model_validate({
        "name": "x", "type": "chat", "protocol": "rag_standard",
        "endpoint": "http://example.com", "session_managed_by": "external",
    })
    assert manifest.session_managed_by == "external"


def test_tool_manifest_requires_invoke():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"name": "x", "type": "tool"})


def test_tool_manifest_invoke_endpoint_must_be_http():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "tool",
            "invoke": {"endpoint": "ftp://example.com"},
        })


def test_tool_manifest_invoke_timeout_capped_at_120():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "tool",
            "invoke": {"endpoint": "http://example.com", "timeout_seconds": 121},
        })


def test_tool_manifest_api_key_header_requires_header_name():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "tool",
            "invoke": {
                "endpoint": "http://example.com",
                "auth": {"type": "api_key_header", "token": "secret"},
            },
        })


def test_select_field_requires_options():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "tool",
            "invoke": {"endpoint": "http://example.com"},
            "input_schema": {
                "order": ["line"],
                "fields": {"line": {"type": "string", "ui": "select", "label": "产线"}},
            },
        })


def test_input_schema_order_must_match_fields():
    with pytest.raises(ValidationError):
        ManifestInputSchema.model_validate({
            "order": ["line", "missing_field"],
            "fields": {"line": {"type": "string", "ui": "input", "label": "产线"}},
        })


def test_max_mb_capped_at_50():
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({
            "name": "x", "type": "tool",
            "invoke": {"endpoint": "http://example.com"},
            "input_schema": {
                "order": ["file"],
                "fields": {"file": {"type": "file", "ui": "upload", "label": "文件", "max_mb": 51}},
            },
        })


def test_valid_tool_manifest_passes():
    manifest = PluginManifest.model_validate({
        "name": "热压缺陷知识问答", "type": "tool",
        "invoke": {
            "endpoint": "http://10.50.54.233:9000/api/v1/defects/query",
            "method": "POST",
            "auth": {"type": "api_key_header", "header_name": "X-API-Key", "token": "secret"},
        },
        "input_schema": {
            "order": ["line", "file"],
            "fields": {
                "line": {"type": "string", "ui": "select", "label": "产线", "required": True,
                          "options": ["热压1线", "热压2线"]},
                "file": {"type": "file", "ui": "upload", "label": "明细文件",
                          "accept": [".xlsx", ".csv"], "max_mb": 20},
            },
        },
        "output_hint": {"display": "table", "items_path": "data", "primary_key": "defect_code"},
    })
    assert manifest.type == "tool"
    assert manifest.invoke.pass_mode == "multipart"


# ── build_dynamic_input_model：动态校验模型 ──────────────────────────────────

def test_dynamic_input_model_enforces_required_fields():
    schema = ManifestInputSchema.model_validate({
        "order": ["line", "note"],
        "fields": {
            "line": {"type": "string", "ui": "select", "label": "产线", "required": True,
                      "options": ["热压1线"]},
            "note": {"type": "string", "ui": "input", "label": "备注", "required": False},
        },
    })
    dynamic_model = build_dynamic_input_model(schema)

    with pytest.raises(ValidationError):
        dynamic_model.model_validate({"note": "hi"})  # 缺必填 line

    instance = dynamic_model.model_validate({"line": "热压1线"})
    assert instance.line == "热压1线"
    assert instance.note is None


# ── POST /plugins/validate：HTTP 端点 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_requires_operator_role(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "x", "type": "chat", "protocol": "openai_compatible",
            "endpoint": "http://127.0.0.1:1",
        })
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_validate_returns_schema_errors_without_network_call(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "x", "type": "chat",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is False
        assert data["schema_errors"]
        assert data["connectivity"] is None
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_validate_chat_unreachable_endpoint_returns_warning(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "x", "type": "chat", "protocol": "rag_standard",
            "endpoint": "http://127.0.0.1:1",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is True
        assert data["connectivity"]["ok"] is False
        assert any("不可达" in w for w in data["warnings"])
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_validate_tool_post_without_defaults_skips_invoke(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/v1/external-agents/plugins/validate", json={
            "name": "x", "type": "tool",
            "invoke": {"endpoint": "http://127.0.0.1:1", "method": "POST"},
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_ok"] is True
        assert data["connectivity"]["skipped_invoke"] is True
        assert any("未实际调用接口" in w for w in data["warnings"])
    finally:
        _clear_override()


# ── Phase 2 §4.1：ManifestFieldSchema 字段级 description / default 校验 ────────
#
# 任务 0 补齐的两个字段（description / default）。约束：
#   - default 类型必须与 type 匹配（string→str / number→int|float / boolean→bool
#     / object→dict|list；file 不允许 default）
#   - number 严格拒绝 bool（Python 中 bool 是 int 子类）
#   - description / default 同时设置 required 不阻断
#   - build_dynamic_input_model 在可选字段缺省时用 field.default 兜底
# -----------------------------------------------------------------------------


def test_field_default_string_accepted():
    """string 字段接收字符串 default。"""
    schema = ManifestInputSchema.model_validate({
        "order": ["line"],
        "fields": {"line": {"type": "string", "ui": "input", "label": "产线",
                              "default": "热压1线"}},
    })
    assert schema.fields["line"].default == "热压1线"


def test_field_default_number_accepted():
    """number 字段接收 int / float default。"""
    for raw in (42, 3.14):
        schema = ManifestInputSchema.model_validate({
            "order": ["qty"],
            "fields": {"qty": {"type": "number", "ui": "number", "label": "数量",
                                "default": raw}},
        })
        assert schema.fields["qty"].default == raw


def test_field_default_boolean_accepted():
    """boolean 字段接收 true / false default。"""
    for raw in (True, False):
        schema = ManifestInputSchema.model_validate({
            "order": ["enabled"],
            "fields": {"enabled": {"type": "boolean", "ui": "switch", "label": "启用",
                                    "default": raw}},
        })
        assert schema.fields["enabled"].default is raw


def test_field_default_object_accepted():
    """object 字段接收 dict / list default。"""
    schema = ManifestInputSchema.model_validate({
        "order": ["meta"],
        "fields": {"meta": {"type": "object", "ui": "textarea", "label": "元数据",
                             "default": {"k": "v"}}},
    })
    assert schema.fields["meta"].default == {"k": "v"}


def test_field_default_type_mismatch_rejected():
    """string 字段给 number default 应校验失败。"""
    with pytest.raises(ValidationError) as ei:
        ManifestInputSchema.model_validate({
            "order": ["qty"],
            "fields": {"qty": {"type": "number", "ui": "number", "label": "数量",
                                "default": "hello"}},
        })
    assert "不匹配" in str(ei.value)


def test_field_default_bool_not_accepted_as_number():
    """bool 在 Python 中是 int 子类，但 number 字段必须严格拒绝（避免 True→1 误传）。"""
    with pytest.raises(ValidationError) as ei:
        ManifestInputSchema.model_validate({
            "order": ["qty"],
            "fields": {"qty": {"type": "number", "ui": "number", "label": "数量",
                                "default": True}},
        })
    assert "不匹配" in str(ei.value)


def test_field_default_file_rejected():
    """file 类型字段不允许 default（每次上传需用户重新选）。"""
    with pytest.raises(ValidationError) as ei:
        ManifestInputSchema.model_validate({
            "order": ["upload"],
            "fields": {"upload": {"type": "file", "ui": "upload", "label": "文件",
                                   "default": "should-not-allow"}},
        })
    assert "file 不允许提供 default" in str(ei.value)


def test_field_description_accepted():
    """description 字段接受任意字符串（字段作用说明）。"""
    schema = ManifestInputSchema.model_validate({
        "order": ["line"],
        "fields": {"line": {"type": "string", "ui": "input", "label": "产线",
                              "description": "产线名称，如：热压1线"}},
    })
    assert schema.fields["line"].description == "产线名称，如：热压1线"


def test_dynamic_input_model_uses_default_when_field_missing():
    """可选字段缺省时，动态模型用 field.default 兜底（服务端兜底路径）。"""
    schema = ManifestInputSchema.model_validate({
        "order": ["line"],
        "fields": {"line": {"type": "string", "ui": "input", "label": "产线",
                              "default": "热压1线"}},
    })
    dynamic_model = build_dynamic_input_model(schema)
    instance = dynamic_model.model_validate({})
    assert instance.line == "热压1线"


def test_dynamic_input_model_default_does_not_override_provided_value():
    """用户显式传值时，default 不得覆盖（用户输入优先）。"""
    schema = ManifestInputSchema.model_validate({
        "order": ["line"],
        "fields": {"line": {"type": "string", "ui": "input", "label": "产线",
                              "default": "热压1线"}},
    })
    dynamic_model = build_dynamic_input_model(schema)
    instance = dynamic_model.model_validate({"line": "热压2线"})
    assert instance.line == "热压2线"
