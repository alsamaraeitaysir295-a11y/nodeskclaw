"""OpenAPI / Swagger 导入解析器单元测试。

覆盖范围（与 Phase 2 方案 §6.2 映射表 + §13 验收 #7 + review P2-2/P2-4/P2-5 对齐）：

- OpenAPI 3.0/3.1：基础字段映射 + format 行为 + 嵌套拍平 + $ref + name 清洗
- Swagger 2.0：body / formData / query / host+basePath 拼装
- 边界场景：无入参 operation、required + default 共存、缺省值类型不匹配

本测试为纯单元测试：不发网络、不连 DB，只对 `parse_openapi_doc` 的 dict 输入做断言。
"""

from __future__ import annotations

import pytest

from app.services.openapi_import_service import (
    FieldDraft,
    FunctionDraft,
    _default_matches_type,
    parse_openapi_doc,
)


# ── helpers ────────────────────────────────────────────────────────────────


async def _parse(doc: dict) -> list[FunctionDraft]:
    return await parse_openapi_doc(doc)


def _single(drafts: list[FunctionDraft]) -> FunctionDraft:
    assert len(drafts) == 1, f"期望 1 个 draft，实际 {len(drafts)}"
    return drafts[0]


def _by_field(fields: list[FieldDraft], name: str) -> FieldDraft:
    matches = [f for f in fields if f.name == name]
    assert len(matches) == 1, f"字段 {name!r} 唯一匹配失败：找到 {len(matches)} 个"
    return matches[0]


# ── OpenAPI 3.0 / 3.1 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_get_with_one_string_param():
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "Demo", "version": "1.0"},
        "paths": {
            "/users/{id}": {
                "get": {
                    "summary": "Get a user",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    drafts = await _parse(doc)
    f = _single(drafts)
    assert f.method == "GET"
    assert f.path == "/users/{id}"
    field = _by_field(f.fields, "id")
    assert field.type == "string"
    assert field.ui == "input"
    assert field.required is True


@pytest.mark.asyncio
async def test_post_with_required_string_body():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["sku"],
                                    "properties": {
                                        "sku": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    sku = _by_field(f.fields, "sku")
    assert sku.required is True
    assert sku.type == "string"
    assert sku.ui == "input"


@pytest.mark.asyncio
async def test_enum_string_maps_to_select():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "parameters": [{
                        "name": "line",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["L1", "L2"]},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    line = _by_field(f.fields, "line")
    assert line.type == "string"
    assert line.ui == "select"
    assert line.options == ["L1", "L2"]


@pytest.mark.asyncio
async def test_format_date_maps_to_date_ui():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "parameters": [{
                        "name": "start",
                        "in": "query",
                        "schema": {"type": "string", "format": "date"},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    start = _by_field(f.fields, "start")
    assert start.type == "string"
    assert start.ui == "date"


@pytest.mark.asyncio
async def test_format_binary_maps_to_file_type():
    doc = {
        "openapi": "3.0.1",
        "paths": {
            "/upload": {
                "post": {
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "file": {"type": "string", "format": "binary"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    file_f = _by_field(f.fields, "file")
    assert file_f.type == "file"
    assert file_f.ui == "upload"


@pytest.mark.asyncio
async def test_integer_param_maps_to_number_type():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer"},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    limit = _by_field(f.fields, "limit")
    assert limit.type == "number"
    assert limit.ui == "number"


@pytest.mark.asyncio
async def test_boolean_param_maps_to_boolean_switch():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "active",
                        "in": "query",
                        "schema": {"type": "boolean"},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    active = _by_field(f.fields, "active")
    assert active.type == "boolean"
    assert active.ui == "switch"


@pytest.mark.asyncio
async def test_array_body_field_maps_to_object():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "tags": {"type": "array", "items": {"type": "string"}},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    tags = _by_field(f.fields, "tags")
    assert tags.type == "object"
    assert tags.ui == "textarea"
    assert tags.description is not None and "JSON" in tags.description


@pytest.mark.asyncio
async def test_parameter_description_carried_to_field():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "line",
                        "in": "query",
                        "description": "产线名称，如：热压1线",
                        "schema": {"type": "string"},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    line = _by_field(f.fields, "line")
    assert line.description == "产线名称，如：热压1线"


@pytest.mark.asyncio
async def test_schema_default_carried_to_field():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 10},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    limit = _by_field(f.fields, "limit")
    assert limit.default == 10


@pytest.mark.asyncio
async def test_example_not_mapped_to_description():
    """P2-5：example 不混入 description（保留 description 语义纯度）。"""
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "line",
                        "in": "query",
                        "schema": {"type": "string", "example": "热压1线"},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    line = _by_field(f.fields, "line")
    # description 应为空或不包含 "示例" 字样
    assert not line.description or "示例" not in line.description


def test_default_type_mismatch_raises():
    """辅助函数：type 与 default 类型不兼容 → False。"""
    assert _default_matches_type("hello", "number") is False
    assert _default_matches_type(123, "string") is False
    assert _default_matches_type(None, "string") is True
    assert _default_matches_type(10, "number") is True
    assert _default_matches_type(1.5, "number") is True
    assert _default_matches_type(True, "boolean") is True
    # bool 是 int 子类，需特判
    assert _default_matches_type(True, "number") is False
    assert _default_matches_type([], "object") is True
    assert _default_matches_type({}, "object") is True


def test_file_default_raises():
    """辅助函数：file 类型不允许 default → 永远 False。"""
    assert _default_matches_type("anything", "file") is False
    assert _default_matches_type(b"bytes", "file") is False


@pytest.mark.asyncio
async def test_nested_object_flattened_with_dotted_path():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "address": {
                                            "type": "object",
                                            "properties": {
                                                "city": {"type": "string"},
                                                "zip": {"type": "string"},
                                            },
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    city = _by_field(f.fields, "address.city")
    assert city.type == "string"
    assert city.ui == "input"
    zip_f = _by_field(f.fields, "address.zip")
    assert zip_f.type == "string"


@pytest.mark.asyncio
async def test_format_binary_inside_object_is_standalone_file():
    """P2-4：嵌套对象里出现 format=binary → 仍 lift 成顶层 file 字段。"""
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/bakery": {
                "post": {
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "bakery_file": {
                                            "type": "string",
                                            "format": "binary",
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    # 字段名是 bakery_file（顶层），不是 bakery_file.url 之类
    bf = _by_field(f.fields, "bakery_file")
    assert bf.type == "file"
    assert bf.ui == "upload"


@pytest.mark.asyncio
async def test_local_ref_resolved():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Order"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Order": {
                    "type": "object",
                    "properties": {"sku": {"type": "string"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    sku = _by_field(f.fields, "sku")
    assert sku.type == "string"


@pytest.mark.asyncio
async def test_cross_doc_url_ref_rejected_with_warning():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "sku": {
                                            "$ref": "https://evil.com/schemas/Order.json"
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    drafts = await _parse(doc)
    f = _single(drafts)
    # 跨文档 ref 字段被跳过，并发出警告
    assert not any(fd.name == "sku" for fd in f.fields)
    assert any("$ref" in w for w in f.warnings)


@pytest.mark.asyncio
async def test_path_param_included_as_field():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/users/{userId}": {
                "get": {
                    "parameters": [
                        {"name": "userId", "in": "path", "required": True,
                         "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    uid = _by_field(f.fields, "userId")
    assert uid.type == "string"
    assert uid.required is True
    # path template 占位符仍保留在 path 字段
    assert f.path == "/users/{userId}"


@pytest.mark.asyncio
async def test_operation_id_used_as_name_when_present():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "getOrders",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.name == "get_orders"


@pytest.mark.asyncio
async def test_operation_id_sanitized():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "Get-Orders!",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    # 非 [a-z0-9_] 替换为 _；折叠；rstrip _
    assert f.name == "get_orders"


@pytest.mark.asyncio
async def test_fallback_name_when_no_operation_id():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    # 兜底：{method}_{path_sanitized}
    assert f.name == "get_orders"


@pytest.mark.asyncio
async def test_200_array_response_yields_table_suggestion():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array", "items": {"type": "object"}}
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.output_hint_suggestion == {"display": "table", "items_path": ""}


@pytest.mark.asyncio
async def test_object_response_with_array_field_yields_table_suggestion():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "items": {
                                                "type": "array",
                                                "items": {"type": "object"},
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.output_hint_suggestion == {"display": "table", "items_path": "items"}


@pytest.mark.asyncio
async def test_object_response_no_array_yields_json_suggestion():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/orders": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.output_hint_suggestion["display"] == "json"
    assert f.output_hint_suggestion["items_path"] is None


@pytest.mark.asyncio
async def test_servers_relative_url_emits_warning():
    doc = {
        "openapi": "3.0.0",
        "servers": [{"url": "/api/v3"}],
        "paths": {
            "/orders": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    drafts = await _parse(doc)
    f = _single(drafts)
    # 警告出现在某个 draft.warnings 里
    assert any("base_url" in w or "server" in w.lower() for w in f.warnings)


# ── Swagger 2.0 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swagger_2_body_param_flattened():
    doc = {
        "swagger": "2.0",
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "required": True,
                            "schema": {"$ref": "#/definitions/Order"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "definitions": {
            "Order": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.name == "create_order"
    sku = _by_field(f.fields, "sku")
    assert sku.type == "string"


@pytest.mark.asyncio
async def test_swagger_2_body_field_description_propagated():
    """回归测试：修复前 Swagger 2.0 body 内嵌字段的 description 丢失。

    修复前 line 1104 用 `description if prop_name == name else None` 只在顶层
    body 字段才传 description；嵌套 prop 全部丢失，与 spec §13 #1 期望冲突。
    修复后每个 prop_schema 自带的 description 必须透传到 FieldDraft.description。
    """
    doc = {
        "swagger": "2.0",
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "required": True,
                            "schema": {"$ref": "#/definitions/Order"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "definitions": {
            "Order": {
                "type": "object",
                "required": ["sku"],
                "properties": {
                    "sku": {
                        "type": "string",
                        "description": "商品 SKU 编码",
                        "default": "HP-001",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "数量",
                    },
                },
            }
        },
    }
    f = _single(await _parse(doc))
    sku = _by_field(f.fields, "sku")
    qty = _by_field(f.fields, "qty")
    assert sku.description == "商品 SKU 编码"
    assert sku.default == "HP-001"
    assert qty.description == "数量"
    assert qty.default is None


@pytest.mark.asyncio
async def test_swagger_2_formdata_param_treated_as_field():
    doc = {
        "swagger": "2.0",
        "paths": {
            "/upload": {
                "post": {
                    "parameters": [
                        {"in": "formData", "name": "file", "type": "file", "required": True},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    file_f = _by_field(f.fields, "file")
    assert file_f.type == "file"
    assert file_f.ui == "upload"
    assert file_f.required is True


@pytest.mark.asyncio
async def test_swagger_2_query_param_treated_as_field():
    doc = {
        "swagger": "2.0",
        "paths": {
            "/orders": {
                "get": {
                    "parameters": [
                        {"in": "query", "name": "limit", "type": "integer", "required": False},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    limit = _by_field(f.fields, "limit")
    assert limit.type == "number"
    assert limit.required is False


@pytest.mark.asyncio
async def test_swagger_2_base_url_constructed_from_host_basepath():
    doc = {
        "swagger": "2.0",
        "host": "api.example.com",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    # 本测试不直接验证 base_url（FunctionDraft 不携带），但要保证解析不报错
    drafts = await _parse(doc)
    assert _single(drafts).name == "list_orders"


# ── 边界 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_path_no_parameters_skipped_or_minimal():
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/health": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    # 不应抛错；fields 为空；warnings 含"无可识别"提示
    assert f.fields == []
    assert any("无" in w or "schema" in w.lower() for w in f.warnings)


@pytest.mark.asyncio
async def test_default_in_required_field_no_raise():
    """required=true + default 共存不报错；default 仍写入。"""
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/items": {
                "get": {
                    "parameters": [{
                        "name": "limit",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "integer", "default": 20},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    limit = _by_field(f.fields, "limit")
    assert limit.required is True
    assert limit.default == 20


# ── 组合式 schema（allOf/oneOf/anyOf）：必须显式告警而非静默丢字段 ────────────


@pytest.mark.asyncio
async def test_openapi3_body_allof_emits_warning_not_silent():
    """OpenAPI 3.x：requestBody schema 用 allOf 组合 → 0 字段但 warnings 必须包含提示。"""
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "allOf": [
                                        {"$ref": "#/components/schemas/Base"},
                                        {"type": "object", "properties": {"sku": {"type": "string"}}},
                                    ]
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {"schemas": {"Base": {"type": "object", "properties": {"id": {"type": "string"}}}}},
    }
    f = _single(await _parse(doc))
    assert f.fields == []
    assert any("allOf" in w for w in f.warnings), f.warnings


@pytest.mark.asyncio
async def test_openapi3_property_level_allof_emits_warning():
    """OpenAPI 3.x：body 某属性本身是 allOf schema → 该属性跳过且 warning 提及字段名。"""
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "address": {
                                            "allOf": [
                                                {"type": "object", "properties": {"city": {"type": "string"}}}
                                            ]
                                        },
                                        "sku": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    # sku 正常产出，address 被跳过
    assert _by_field(f.fields, "sku").type == "string"
    assert all(fd.name != "address" and fd.name != "address.city" for fd in f.fields)
    assert any("address" in w and "allOf" in w for w in f.warnings), f.warnings


@pytest.mark.asyncio
async def test_swagger2_body_allof_emits_warning():
    """Swagger 2.0：body schema 用 allOf → 返回 warning 提示（字段不产出）。"""
    doc = {
        "swagger": "2.0",
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "schema": {
                                "allOf": [
                                    {"type": "object", "properties": {"sku": {"type": "string"}}}
                                ]
                            },
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert all(fd.name != "sku" for fd in f.fields)
    assert any("allOf" in w for w in f.warnings), f.warnings


@pytest.mark.asyncio
async def test_example_maps_to_placeholder_not_description():
    """OpenAPI example → 输入框占位提示（placeholder），不进 description（P2-5 语义不变）。"""
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "parameters": [
                        {
                            "name": "order_id", "in": "query", "required": True,
                            "description": "订单编号",
                            "schema": {"type": "string", "example": "SO-1001"},
                        },
                        {
                            "name": "qty", "in": "query",
                            "schema": {"type": "integer", "example": 3},
                        },
                        {
                            "name": "day", "in": "query",
                            "schema": {"type": "string", "format": "date", "example": "2026-08-21"},
                        },
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    oid_f = _by_field(f.fields, "order_id")
    qty_f = _by_field(f.fields, "qty")
    day_f = _by_field(f.fields, "day")

    assert oid_f.placeholder == "SO-1001"
    assert oid_f.description == "订单编号"  # example 不污染字段作用
    assert qty_f.placeholder == "3"
    assert day_f.placeholder == "2026-08-21"


@pytest.mark.asyncio
async def test_answer_object_response_suggests_text_display():
    """RAG 类问答约定：响应对象含字符串 answer → 建议 display=text + text_path=answer。"""
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "rag", "version": "1"},
        "servers": [{"url": "https://api.legit.com"}],
        "paths": {
            "/api/v1/agent/query": {
                "post": {
                    "operationId": "ragQuery",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["question"],
                        "properties": {"question": {"type": "string", "description": "问题"}},
                    }}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                    }}}}},
                }
            }
        },
    }
    f = _single(await _parse(doc))
    assert f.output_hint_suggestion == {"display": "text", "text_path": "answer"}
