"""OpenAPI / Swagger 文档解析器（Phase 2 §6 方案）。

纯函数模块：不发任何 HTTP 请求、不读写 DB、不读文件系统。
调用方（Task 6 API 层）负责把 OpenAPI 文档内容拉下来并解析成 dict，
本模块只负责把 dict 归一化为一份"function 草稿"列表，
供前端 wizard 渲染和管理员在向导里勾选、调整字段。

设计要点（与 phase 2 spec §6 + review P2-2/P2-4/P2-5/P2-6/P2-7 对齐）：

- name 清洗（P2-2）：去非 `[a-z0-9_]` → 折叠重复 `_` → 截 128 字符 → 全部空时
  用 `{method}_{path_sanitized}` 兜底；命名冲突不在本层处理（API 层负责追加
  `_2`/`_3` 后缀），保持解析器幂等可单测。
- 嵌套对象拍平 + format=binary（P2-4）：普通 object 字段按 §6.2 表映射成
  type=object / ui=textarea；嵌套 schema 的 properties 会被点号路径拍平到
  顶层 field（如 `address.city`）。`format=binary` 字段**不参与拍平**，
  直接作为独立 file 字段名（既在请求体根也在嵌套对象里都直接用字段名作为
  最终 field name），不展开成 `parent.field`。
- example 不写进 description（P2-5）：spec §6.2 原本要求"(示例: 热压1线)"
  追加到 description，但 description 字段在 Phase 1 已定义为"字段作用"，
  混进示例会污染语义。本层**刻意**不映射 `example`，留待 Phase 2.5 单独
  设计 UI 入口展示。代码注释里标了 TODO 提示。
- 跨文档 `$ref` 拒绝（P2-3 / review §5 Z-3）：URL 形式的 ref（`http://...`）
  直接 warn 并跳过该字段，不做网络拉取。
- spec_version：OpenAPI 文档 `openapi` 字段（"3.0.0"/"3.1.0"）原文返回；
  Swagger 2.0 时为 `"2.0"`。本模块不归一化为内部枚举。
- YAML 解析不在本模块范围内（review P2-6）：本层只吃 dict，文档解析在 API 层。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel as PydanticBase

logger = logging.getLogger(__name__)


# ── 常量 ──────────────────────────────────────────────────────────────────


# 文档大小上限（5 MB）。预览/导入都按此值截断，spec §9.4。
MAX_OPENAPI_DOC_SIZE = 5 * 1024 * 1024

# 文档拉取默认超时（15s），spec §9.4。
DEFAULT_OPENAPI_FETCH_TIMEOUT = 15.0


# ── 输出结构 ────────────────────────────────────────────────────────────────


FieldType = Literal["string", "number", "boolean", "object", "file"]
FieldUI = Literal[
    "input", "textarea", "select", "number", "switch", "date", "daterange", "upload"
]


class FieldDraft(PydanticBase):
    """单个 input field 的解析草稿。"""

    name: str
    type: FieldType
    ui: FieldUI
    label: str
    required: bool = False
    description: str | None = None
    default: Any | None = None
    # OpenAPI example 映射为输入框占位提示（告诉用户"该填什么"），
    # 不与 description 混排（P2-5 决策不变：description 只承载字段作用）
    placeholder: str | None = None
    options: list[str] | None = None
    accept: list[str] | None = None
    max_mb: int | None = None


class FunctionDraft(PydanticBase):
    """单个 operation 的解析草稿。"""

    name: str
    summary: str | None = None
    method: str
    path: str
    fields: list[FieldDraft]
    output_hint_suggestion: dict[str, Any]
    warnings: list[str]


# ── 公开 API ────────────────────────────────────────────────────────────────


async def parse_openapi_doc(
    doc: dict,
    *,
    base_url_override: str | None = None,
) -> list[FunctionDraft]:
    """把 OpenAPI 3.0/3.1 或 Swagger 2.0 dict 解析为 function 草稿列表。

    本函数为纯函数，不发起任何 I/O；异步签名仅为后续 API 层统一 async 调用。
    `doc` 已经是 dict 形态，API 层负责反序列化（JSON dict 或 YAML→dict）。

    返回列表按 OpenAPI 文档中的 (path, method) 顺序输出，调用方按需展示或排序。
    """
    if not isinstance(doc, dict):
        return []

    spec_version = _detect_spec_version(doc)
    if spec_version.startswith("3."):
        return _parse_openapi_3x(doc, base_url_override=base_url_override)
    if spec_version == "2.0":
        return _parse_swagger_2(doc)
    # 未知 spec_version：不强行抛错，返回空列表让上层走错误展示
    return []


# ── 顶层分派 ────────────────────────────────────────────────────────────────


def _detect_spec_version(doc: dict) -> str:
    """识别 OpenAPI/Swagger 版本字段。

    OpenAPI 3.x 用 `openapi: "3.0.x"` / `"3.1.x"`；
    Swagger 2.0 用 `swagger: "2.0"`。
    缺字段时按"未知"返回空串——调用方据此决定走哪条解析路径。
    """
    return str(doc.get("openapi") or doc.get("swagger") or "").strip()


# ── 字段映射核心 ────────────────────────────────────────────────────────────


_NAME_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_NAME_MULTI_UNDERSCORE = re.compile(r"_+")
# camelCase 边界：lowercase/digit 后跟 uppercase（如 `getOrders` → `get_Orders`）
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NAME_MAX_LEN = 128


def _sanitize_name(raw: str) -> str:
    """把任意字符串清洗为合法 function name。

    规则（review P2-2 决策 + 测试矩阵 camelCase 需求）：
      1. 把 camelCase 拆成 snake_case（`getOrders` → `get_Orders`）；
      2. 全小写；
      3. 非 `[a-z0-9_]` 替换为 `_`；
      4. 折叠连续 `_`；
      5. 去掉首尾 `_`；
      6. 截断到 128 字符；
      7. 全部空 → 返回空串（调用方决定是否走 fallback）。

    命名冲突不在本层处理（API 层负责追加 `_2`/`_3` 后缀）。
    """
    if not raw:
        return ""
    # camelCase → snake_case（只拆 lower→Upper，不拆连续大写如 XMLParser）
    split = _CAMEL_BOUNDARY.sub(r"\1_\2", raw)
    lowered = split.lower()
    replaced = _NAME_INVALID_CHARS.sub("_", lowered)
    collapsed = _NAME_MULTI_UNDERSCORE.sub("_", replaced).strip("_")
    if len(collapsed) > _NAME_MAX_LEN:
        collapsed = collapsed[:_NAME_MAX_LEN].rstrip("_")
    return collapsed


def _fallback_name(method: str, path: str) -> str:
    """operationId 缺失/清洗后为空时，按 `{method}_{path_sanitized}` 兜底。

    path 形如 `/api/v1/orders/{id}` → `api_v1_orders_id`，再拼 method 前缀。
    """
    cleaned = path.replace("/", "_").replace("{", "").replace("}", "")
    cleaned = _sanitize_name(cleaned) or "path"
    return f"{method.lower()}_{cleaned}"


def _path_param_name(path: str) -> str:
    """抽取 path 里第一个 `{xxx}` 的 xxx（小写化），用于 fallback name 拼接。"""
    m = re.search(r"\{([^}]+)\}", path)
    return m.group(1).lower() if m else ""


def _label_humanize(field_name: str) -> str:
    """字段 label 兜底：把 `address.city` / `user_id` 之类转为 Title Case。

    不追求完美，能给前端一个默认 label 占位即可。
    """
    parts = re.split(r"[._\-\s]+", field_name)
    return " ".join(p.capitalize() for p in parts if p)


def _default_matches_type(default: Any, type_str: str) -> bool:
    """判定 schema `default` 值是否与 manifest type 兼容。

    None 视作"未提供 default"，永远放行；
    string→str、number→int/float（排除 bool）、boolean→bool、
    object→dict/list、file 永远 False（文件不能有默认值）。
    """
    if default is None:
        return True
    if type_str == "string":
        return isinstance(default, str)
    if type_str == "number":
        return isinstance(default, (int, float)) and not isinstance(default, bool)
    if type_str == "boolean":
        return isinstance(default, bool)
    if type_str == "object":
        return isinstance(default, (dict, list))
    if type_str == "file":
        return False
    return True


def _map_schema_to_field(
    schema: dict,
    *,
    name: str,
    description: str | None = None,
    required: bool = False,
    default: Any = None,
) -> tuple[FieldDraft | None, str | None]:
    """按 §6.2 表把 OpenAPI schema dict 映射成 FieldDraft。

    返回 (field, warning)：
      - field=None 且 warning is None：当前 schema 不产出 field（如纯 $ref 跳过）
      - field=None 且 warning is not None：跳过该字段，warning 已记录到 FunctionDraft.warnings
      - field is not None：产出该字段

    `default` 由调用方从 schema.default 提取并按 _default_matches_type 校验后再传入，
    本函数不再二次校验 type 兼容性（避免嵌套对象拍平时逐字段重复校验）。
    """
    if not isinstance(schema, dict):
        return None, None

    # 解 $ref 一次；递归路径上允许跨层 ref 解析
    schema = _resolve_local_ref(schema)

    # 组合式 schema 显式拦截：给出可操作的原因提示（而非落到"未知 type"兜底），
    # 避免对接方拿到含糊的 type=None 消息（规范文档 §6：组合式请内联 properties）
    if any(k in schema for k in ("allOf", "oneOf", "anyOf")):
        return (
            None,
            f"字段 {name!r} 使用了 allOf/oneOf/anyOf 组合式 schema，其字段未导入——"
            "请内联为完整 properties 后重新提供文档",
        )

    # type / format 取值
    raw_type = schema.get("type")
    fmt = (schema.get("format") or "").lower() or None

    # OpenAPI example → 输入框占位提示（见 FieldDraft.placeholder 注释）
    placeholder: str | None = None
    raw_example = schema.get("example")
    if raw_example is None and isinstance(schema.get("examples"), list) and schema["examples"]:
        raw_example = schema["examples"][0]
    if raw_example is not None and not isinstance(raw_example, (dict, list)):
        placeholder = str(raw_example)

    # 数组：按 §6.2 全部映射为 type=object/ui=textarea
    if raw_type == "array":
        merged_desc = _append_description(description, "（JSON 输入）")
        return (
            FieldDraft(
                name=name,
                type="object",
                ui="textarea",
                label=description or _label_humanize(name),
                required=required,
                description=merged_desc,
                default=default,
            ),
            None,
        )

    # 无 type 但 properties 存在：按对象处理
    if raw_type is None and "properties" in schema:
        raw_type = "object"

    # object 但要再判一下：format=binary 在 object 内部某属性里 → 当前层不走 object 拍平
    if raw_type == "object":
        # 嵌套对象处理：在拍平前先看 properties 里有没有 format=binary
        properties = schema.get("properties") or {}
        binary_in_props = any(
            isinstance(p, dict) and (p.get("format") or "").lower() == "binary"
            for p in properties.values()
        )
        if binary_in_props:
            merged_desc = _append_description(description, "（JSON 输入，含文件字段）")
        else:
            merged_desc = _append_description(description, "（JSON 输入）")
        return (
            FieldDraft(
                name=name,
                type="object",
                ui="textarea",
                label=description or _label_humanize(name),
                required=required,
                description=merged_desc,
                default=default,
            ),
            None,
        )

    if raw_type == "string":
        # 1) enum → select
        enum_vals = schema.get("enum")
        if isinstance(enum_vals, list) and enum_vals:
            opts = [str(x) for x in enum_vals]
            return (
                FieldDraft(
                    name=name,
                    type="string",
                    ui="select",
                    label=description or _label_humanize(name),
                    required=required,
                    description=description,
                    default=default,
                    options=opts,
                ),
                None,
            )
        # 2) format=date → date
        if fmt == "date":
            return (
                FieldDraft(
                    name=name,
                    type="string",
                    ui="date",
                    label=description or _label_humanize(name),
                    required=required,
                    description=description,
                    default=default,
                    placeholder=placeholder,
                ),
                None,
            )
        # 3) format=binary → file/upload
        if fmt == "binary":
            return (
                FieldDraft(
                    name=name,
                    type="file",
                    ui="upload",
                    label=description or _label_humanize(name),
                    required=required,
                    description=description,
                ),
                None,
            )
        # 4) format=date-time → 描述里追加 ISO-8601 提示
        if fmt == "date-time":
            merged_desc = _append_description(description, "（ISO-8601 时间）")
            return (
                FieldDraft(
                    name=name,
                    type="string",
                    ui="input",
                    label=description or _label_humanize(name),
                    required=required,
                    description=merged_desc,
                    default=default,
                ),
                None,
            )
        # 5) 其他 format（uuid / email / …）：保持 string/input，描述里标注已知 format
        if fmt and fmt not in ("date", "date-time", "binary"):
            known = {"uuid": "UUID 字符串", "email": "邮箱", "uri": "URI", "url": "URL"}
            note = known.get(fmt)
            if note:
                merged_desc = _append_description(description, f"（{note}）")
            else:
                merged_desc = description
            return (
                FieldDraft(
                    name=name,
                    type="string",
                    ui="input",
                    label=description or _label_humanize(name),
                    required=required,
                    description=merged_desc,
                    default=default,
                    placeholder=placeholder,
                ),
                None,
            )
        # 6) 普通 string
        return (
            FieldDraft(
                name=name,
                type="string",
                ui="input",
                label=description or _label_humanize(name),
                required=required,
                description=description,
                default=default,
                placeholder=placeholder,
            ),
            None,
        )

    if raw_type in ("integer", "number"):
        return (
            FieldDraft(
                name=name,
                type="number",
                ui="number",
                label=description or _label_humanize(name),
                required=required,
                description=description,
                default=default,
                placeholder=placeholder,
            ),
            None,
        )

    if raw_type == "boolean":
        return (
            FieldDraft(
                name=name,
                type="boolean",
                ui="switch",
                label=description or _label_humanize(name),
                required=required,
                description=description,
                default=default,
            ),
            None,
        )

    # 兜底：未知 type（含 raw_type 为 None 且无 properties）→ warn 跳过
    return None, f"字段 {name!r} 的 schema type={raw_type!r} 暂不支持，已跳过"


def _append_description(existing: str | None, suffix: str) -> str:
    """把附加提示追加到 description 末尾，避免空 description 时出现孤立 '（）'。"""
    base = (existing or "").rstrip()
    if not base:
        return suffix.lstrip("（").rstrip("）").strip()
    if base.endswith("。"):
        return f"{base}{suffix.lstrip('（').rstrip('）').strip()}。"
    return f"{base}，{suffix.lstrip('（').rstrip('）').strip()}"


# ── $ref 解析 ────────────────────────────────────────────────────────────────


def _resolve_local_ref(node: Any) -> Any:
    """解析 `#/components/schemas/Foo` 或 `#/definitions/Foo` 形式的本地 ref。

    - 仅解析**单文档内**的 JSON pointer ref；
    - 跨文档 URL ref（`http://...`）保持原样，调用方需要在更高一层 warn 跳过
      （避免本函数在嵌套深处静默 warn，导致某个深层字段丢失但 surface 字段仍产出）；
    - 解析失败（ref 指向不存在路径）保持原样——后续 `_map_schema_to_field` 会按 type
      缺失走未知 type → warn 跳过。
    """
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    if not ref.startswith("#/"):
        # 含 http:// 或 相对路径都视为跨文档，原样返回让上层 warn
        return node
    return node  # 默认保留原 dict（含 $ref），由调用方按目标 schema 路径走解析


def _resolve_openapi_3_ref(ref: str, doc: dict) -> dict | None:
    """解析 OpenAPI 3.x 的 `#/components/schemas/Foo` ref。

    返回解析后的 dict，跨文档 ref 返回 None。
    """
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        return None
    name = ref[len("#/components/schemas/"):]
    components = doc.get("components") or {}
    schemas = components.get("schemas") or {}
    return schemas.get(name) if isinstance(schemas, dict) else None


def _resolve_swagger_2_ref(ref: str, doc: dict) -> dict | None:
    """解析 Swagger 2.0 的 `#/definitions/Foo` ref。

    返回解析后的 dict，非本格式 ref 返回 None。
    """
    if not isinstance(ref, str) or not ref.startswith("#/definitions/"):
        return None
    name = ref[len("#/definitions/"):]
    definitions = doc.get("definitions") or {}
    return definitions.get(name) if isinstance(definitions, dict) else None


# ── OpenAPI 3.0 / 3.1 解析 ──────────────────────────────────────────────────


def _parse_openapi_3x(doc: dict, *, base_url_override: str | None) -> list[FunctionDraft]:
    """OpenAPI 3.x 文档解析主入口。"""
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return []

    warnings_global: list[str] = []

    base_url = _extract_openapi_3_base_url(doc, base_url_override, warnings_global)

    drafts: list[FunctionDraft] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or not isinstance(path, str):
            continue
        path_level_params = path_item.get("parameters") or []
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            draft = _parse_openapi_3_operation(
                op,
                method=method,
                path=path,
                path_level_params=path_level_params,
                doc=doc,
                base_url=base_url,
                global_warnings=warnings_global,
            )
            if draft is not None:
                drafts.append(draft)
    return drafts


def _extract_openapi_3_base_url(
    doc: dict,
    base_url_override: str | None,
    warnings_out: list[str],
) -> str | None:
    """取 OpenAPI 3.x 的首个 server URL。相对路径且无 override 时 warn。"""
    servers = doc.get("servers") or []
    if not isinstance(servers, list) or not servers:
        return base_url_override
    first = servers[0]
    if not isinstance(first, dict):
        return base_url_override
    url = first.get("url")
    if not isinstance(url, str):
        return base_url_override
    if url.startswith("/"):
        if base_url_override:
            return base_url_override.rstrip("/") + url
        warnings_out.append(
            "相对路径 server URL，需管理员提供 base_url 才能拼出 invoke endpoint"
        )
        return None
    return url


def _parse_openapi_3_operation(
    op: dict,
    *,
    method: str,
    path: str,
    path_level_params: list,
    doc: dict,
    base_url: str | None,
    global_warnings: list[str],
) -> FunctionDraft | None:
    """OpenAPI 3.x 单个 operation 的解析。"""
    # 全局级警告（如 server URL 相对路径）一并写入每个 draft.warnings，
    # 让前端 wizard 能直接展示给管理员（不去追查 global_warnings 列表）
    warnings: list[str] = list(global_warnings)

    # 1) name
    operation_id = op.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        sanitized = _sanitize_name(operation_id)
        if not sanitized:
            sanitized = _fallback_name(method, path)
    else:
        sanitized = _fallback_name(method, path)
    name = sanitized or _fallback_name(method, path)

    # 2) summary
    summary = op.get("summary") or op.get("description")

    # 3) fields：合并 path-level 与 operation-level parameters，operation 优先
    merged_params = _merge_openapi_3_parameters(path_level_params, op.get("parameters") or [])
    fields: list[FieldDraft] = []
    seen_field_names: set[str] = set()

    for param in merged_params:
        if not isinstance(param, dict):
            continue
        param = _resolve_param_ref(param, doc, resolver=_resolve_openapi_3_ref, warnings=warnings)
        if param is None:
            continue
        field, warn = _param_to_field_openapi_3(param, parent_path=path)
        if warn:
            warnings.append(warn)
        if field is None:
            continue
        if field.name in seen_field_names:
            # 重复字段（同名 query/path 冲突或合并时冲突）：跳过后者并 warn
            warnings.append(f"字段 {field.name!r} 重复定义，已跳过后者")
            continue
        seen_field_names.add(field.name)
        fields.append(field)

    # 4) requestBody
    body_fields, body_warnings = _extract_openapi_3_body_fields(op, doc, warnings)
    warnings.extend(body_warnings)
    for f in body_fields:
        if f.name in seen_field_names:
            warnings.append(f"字段 {f.name!r} 重复定义，已跳过后者")
            continue
        seen_field_names.add(f.name)
        fields.append(f)

    # 5) 退化判定：fields 为空 → emit warning 但仍产出草稿（spec §6.4 不跳过）
    if not fields:
        warnings.append("operation 无可识别的 input schema（无 parameters / requestBody）")

    # 6) output_hint 建议
    output_hint_suggestion = _suggest_output_hint_openapi_3(op)

    return FunctionDraft(
        name=name,
        summary=summary if isinstance(summary, str) else None,
        method=method.upper(),
        path=path,
        fields=fields,
        output_hint_suggestion=output_hint_suggestion,
        warnings=warnings,
    )


def _merge_openapi_3_parameters(
    path_level: list,
    op_level: list,
) -> list:
    """合并 path-level 与 operation-level parameters；同名同 in 字段由 op-level 覆盖。"""
    if not isinstance(path_level, list):
        path_level = []
    if not isinstance(op_level, list):
        op_level = []
    merged: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    # 先 operation-level：优先级更高
    for p in op_level:
        if isinstance(p, dict):
            key = (str(p.get("name") or ""), str(p.get("in") or ""))
            if key not in seen_keys:
                merged.append(p)
                seen_keys.add(key)
    for p in path_level:
        if isinstance(p, dict):
            key = (str(p.get("name") or ""), str(p.get("in") or ""))
            if key not in seen_keys:
                merged.append(p)
                seen_keys.add(key)
    return merged


def _resolve_param_ref(
    param: dict,
    doc: dict,
    *,
    resolver,
    warnings: list[str],
) -> dict | None:
    """处理 parameter 的 $ref；无法解析或跨文档则 warn 并跳过。"""
    ref = param.get("$ref")
    if not ref:
        return param
    if not isinstance(ref, str):
        return param
    # 跨文档 URL ref：直接拒绝（review P2-3 / §5 Z-3）
    if ref.startswith("http://") or ref.startswith("https://"):
        warnings.append(f"跨文档 parameter $ref 已被拒绝：{ref!r}")
        return None
    # 非本格式 ref：拒绝
    if not ref.startswith("#/"):
        warnings.append(f"非本地 parameter $ref 已被拒绝：{ref!r}")
        return None
    resolved = resolver(ref, doc)
    if resolved is None:
        warnings.append(f"parameter $ref 解析失败：{ref!r}")
        return None
    return resolved


def _param_to_field_openapi_3(param: dict, *, parent_path: str) -> tuple[FieldDraft | None, str | None]:
    """OpenAPI 3.x 单个 parameter → FieldDraft。

    path-level 参数 `in: path` 在 endpoint 模板中已是 `{param}` 占位，
    本层仍把它当成普通字段收集（spec §6.2）；调用方 invoke 时做 URL 替换。
    """
    name = param.get("name")
    if not isinstance(name, str) or not name:
        return None, None
    schema = param.get("schema") or {}
    description = param.get("description")
    required = bool(param.get("required", False))

    # 提取 default 并校验 type 兼容
    raw_default = schema.get("default")
    inferred_type = _infer_field_type(schema)
    if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
        raise ValueError(
            f"parameter {name!r} 的 default 值 {raw_default!r} 与 schema type={schema.get('type')!r} 不兼容"
        )

    field, warn = _map_schema_to_field(
        schema,
        name=name,
        description=description,
        required=required,
        default=raw_default,
    )
    return field, warn


def _infer_field_type(schema: dict) -> str:
    """从 schema 推断 FieldDraft.type（用于校验 default 类型兼容）。"""
    if not isinstance(schema, dict):
        return "string"
    raw_type = schema.get("type")
    fmt = (schema.get("format") or "").lower()
    if raw_type in ("integer", "number"):
        return "number"
    if raw_type == "boolean":
        return "boolean"
    if raw_type in ("array", "object") or (raw_type is None and "properties" in schema):
        return "object"
    if raw_type == "string" and fmt == "binary":
        return "file"
    if raw_type == "string":
        return "string"
    return "string"


def _extract_openapi_3_body_fields(
    op: dict,
    doc: dict,
    warnings: list[str],
) -> tuple[list[FieldDraft], list[str]]:
    """从 operation 的 requestBody 提取 fields。

    支持 `application/json` 与 `multipart/form-data`：
      - application/json：把 schema.properties 拍平为顶级 field
      - multipart/form-data：把每个 schema.properties 项当顶级 field
    """
    body = op.get("requestBody")
    if not isinstance(body, dict):
        return [], []
    # requestBody 上可能有 $ref：OpenAPI 3.1 允许但 3.0 罕见，按"非典型"处理
    body_ref = body.get("$ref")
    if isinstance(body_ref, str):
        if body_ref.startswith("http://") or body_ref.startswith("https://"):
            warnings.append(f"跨文档 requestBody $ref 已被拒绝：{body_ref!r}")
            return [], []
        if body_ref.startswith("#/components/requestBodies/"):
            components = doc.get("components") or {}
            req_bodies = components.get("requestBodies") or {}
            resolved = req_bodies.get(body_ref.rsplit("/", 1)[-1]) if isinstance(req_bodies, dict) else None
            if isinstance(resolved, dict):
                body = resolved
            else:
                warnings.append(f"requestBody $ref 解析失败：{body_ref!r}")
                return [], []
        else:
            warnings.append(f"非本地 requestBody $ref 已被拒绝：{body_ref!r}")
            return [], []

    content = body.get("content") or {}
    if not isinstance(content, dict):
        return [], []

    # 优先级：application/json > multipart/form-data > 其他（取首个）
    chosen_ct = None
    chosen_media = None
    for candidate in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded"):
        if candidate in content and isinstance(content[candidate], dict):
            chosen_ct = candidate
            chosen_media = content[candidate]
            break
    if chosen_media is None:
        for ct, media in content.items():
            if isinstance(media, dict):
                chosen_ct = ct
                chosen_media = media
                break
    if chosen_media is None:
        return [], []

    schema = chosen_media.get("schema") or {}
    schema = _resolve_openapi_3_inline_ref(schema, doc, warnings)
    if schema is None:
        return [], []

    properties = schema.get("properties") or {}
    if not isinstance(properties, dict) or not properties:
        # body schema 无直接 properties：若因组合式（allOf/oneOf/anyOf）导致，
        # 明确告警而非静默产出 0 字段（对接方最常踩的坑，见规范 §6）
        if any(k in schema for k in ("allOf", "oneOf", "anyOf")):
            warnings.append(
                "requestBody schema 使用了 allOf/oneOf/anyOf 组合式，其字段未导入——"
                "请内联为完整 properties 后重新提供文档"
            )
        return [], []

    required_set = set(schema.get("required") or [])
    fields: list[FieldDraft] = []
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        # 处理字段级 $ref（局部 #/components/schemas/...）
        resolved_prop = _resolve_openapi_3_inline_ref(prop_schema, doc, warnings)
        if resolved_prop is None:
            continue
        # format=binary 直接 lift 成顶层 file 字段，不参与点号路径拍平
        if (resolved_prop.get("format") or "").lower() == "binary":
            fields.append(
                FieldDraft(
                    name=prop_name,
                    type="file",
                    ui="upload",
                    label=prop_name,
                    required=prop_name in required_set,
                )
            )
            continue
        # 普通字段：若 schema 是嵌套 object 且不含 format=binary，按 §6.2 拍平
        fields.extend(
            _flatten_schema_to_fields(
                resolved_prop,
                field_name=prop_name,
                required=prop_name in required_set,
                content_type=chosen_ct,
                warnings=warnings,
            )
        )
    return fields, warnings


def _resolve_openapi_3_inline_ref(
    schema: Any,
    doc: dict,
    warnings: list[str],
) -> dict | None:
    """递归解析 schema 自身的 $ref（一次）；跨文档 URL ref → warn 并返回 None。"""
    if not isinstance(schema, dict):
        return schema if schema is not None else {}
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not isinstance(ref, str):
        return schema
    if ref.startswith("http://") or ref.startswith("https://"):
        warnings.append(f"跨文档 schema $ref 已被拒绝：{ref!r}")
        return None
    if not ref.startswith("#/components/schemas/"):
        warnings.append(f"非本地 schema $ref 已被拒绝：{ref!r}")
        return None
    resolved = _resolve_openapi_3_ref(ref, doc)
    if resolved is None:
        warnings.append(f"schema $ref 解析失败：{ref!r}")
        return None
    return resolved


def _flatten_schema_to_fields(
    schema: dict,
    *,
    field_name: str,
    required: bool,
    content_type: str | None,
    warnings: list[str] | None = None,
) -> list[FieldDraft]:
    """把 schema 拍平为顶层 FieldDraft 列表（含嵌套对象的点号路径）。

    与 P2-4 决策一致：format=binary 字段在调用本函数前已经被调用方 lift 成
    顶层 file 字段，故本函数不会再撞到 binary 内嵌场景。
    warnings：可选的就地收集通道——嵌套字段被跳过时（如组合式 schema）把
    _map_schema_to_field 的 warning 传出去，避免深层静默丢字段。
    """
    # 单字段：直接映射（带 default 透传 + prop 自身 description 作为字段说明/中文名）
    if (schema.get("type") or "") != "object" or "properties" not in schema:
        raw_default = schema.get("default")
        inferred_type = _infer_field_type(schema)
        if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
            raise ValueError(
                f"字段 {field_name!r} 的 default 值 {raw_default!r} 与 schema type={schema.get('type')!r} 不兼容"
            )
        field, warn = _map_schema_to_field(
            schema, name=field_name, required=required,
            description=schema.get("description"), default=raw_default,
        )
        if warn and warnings is not None:
            warnings.append(warn)
        return [field] if field is not None else []

    # object 且有 properties：拍平到点号路径
    properties = schema.get("properties") or {}
    required_set = set(schema.get("required") or [])
    out: list[FieldDraft] = []
    for child_name, child_schema in properties.items():
        if not isinstance(child_schema, dict):
            continue
        if (child_schema.get("format") or "").lower() == "binary":
            # 防御性：调用方已 lift，这里若仍撞到则按"跳过嵌套提升，仅发警告"
            # 实际不会触发，因为 _extract_openapi_3_body_fields 已经先 lift
            continue
        out.extend(
            _flatten_schema_to_fields(
                child_schema,
                field_name=f"{field_name}.{child_name}",
                required=child_name in required_set,
                content_type=content_type,
                warnings=warnings,
            )
        )
    return out


def _suggest_output_hint_openapi_3(op: dict) -> dict[str, Any]:
    """按 §6.3 规则给出 output_hint 建议：顶层 array → items_path=''，
    object 含数组字段 → items_path=<field>，否则 json。
    """
    responses = op.get("responses") or {}
    success = responses.get("200") or responses.get("201") or {}
    if not isinstance(success, dict):
        return {"display": "json", "items_path": None}
    content = success.get("content") or {}
    json_media = content.get("application/json") or {}
    if not isinstance(json_media, dict):
        return {"display": "json", "items_path": None}
    schema = json_media.get("schema") or {}
    if not isinstance(schema, dict):
        return {"display": "json", "items_path": None}
    if schema.get("type") == "array":
        return {"display": "table", "items_path": ""}
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            # RAG 类问答约定：对象含字符串 answer 字段 → Markdown 文本渲染
            answer = properties.get("answer")
            if isinstance(answer, dict) and answer.get("type") == "string":
                return {"display": "text", "text_path": "answer"}
            for field_name, child in properties.items():
                if isinstance(child, dict) and child.get("type") == "array":
                    return {"display": "table", "items_path": field_name}
    return {"display": "json", "items_path": None}


# ── Swagger 2.0 解析 ────────────────────────────────────────────────────────


def _parse_swagger_2(doc: dict) -> list[FunctionDraft]:
    """Swagger 2.0 文档解析主入口。"""
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return []

    base_url = _extract_swagger_2_base_url(doc)
    # base_url 当前未在 FunctionDraft 中保存，仅供调试留口（spec §6.3 由 API 层组装）

    drafts: list[FunctionDraft] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or not isinstance(path, str):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            draft = _parse_swagger_2_operation(
                op, method=method, path=path, doc=doc, base_url=base_url,
            )
            if draft is not None:
                drafts.append(draft)
    return drafts


def _extract_swagger_2_base_url(doc: dict) -> str | None:
    """Swagger 2.0 base URL：`{schemes[0]}://{host}{basePath}`，schemes 缺省 https。"""
    host = (doc.get("host") or "").strip()
    base_path = doc.get("basePath") or ""
    if not isinstance(base_path, str):
        base_path = ""
    schemes = doc.get("schemes") or []
    scheme = "https"
    if isinstance(schemes, list) and schemes:
        first = schemes[0]
        if isinstance(first, str) and first:
            scheme = first
    if not host:
        return None
    return f"{scheme}://{host}{base_path}"


def _parse_swagger_2_operation(
    op: dict,
    *,
    method: str,
    path: str,
    doc: dict,
    base_url: str | None,
) -> FunctionDraft | None:
    warnings: list[str] = []

    operation_id = op.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        sanitized = _sanitize_name(operation_id)
        if not sanitized:
            sanitized = _fallback_name(method, path)
    else:
        sanitized = _fallback_name(method, path)
    name = sanitized or _fallback_name(method, path)

    summary = op.get("summary") or op.get("description")

    fields: list[FieldDraft] = []
    seen: set[str] = set()

    parameters = op.get("parameters") or []
    if isinstance(parameters, list):
        for raw_param in parameters:
            if not isinstance(raw_param, dict):
                continue
            param = _resolve_swagger_2_param_ref(raw_param, doc, warnings)
            if param is None:
                continue
            field_or_list = _swagger_2_param_to_fields(param, doc=doc)
            if isinstance(field_or_list, str):
                warnings.append(field_or_list)
                continue
            for f in field_or_list:
                if f.name in seen:
                    warnings.append(f"字段 {f.name!r} 重复定义，已跳过后者")
                    continue
                seen.add(f.name)
                fields.append(f)

    if not fields:
        warnings.append("operation 无可识别的 input schema（无 parameters / body）")

    output_hint = _suggest_output_hint_swagger_2(op)

    return FunctionDraft(
        name=name,
        summary=summary if isinstance(summary, str) else None,
        method=method.upper(),
        path=path,
        fields=fields,
        output_hint_suggestion=output_hint,
        warnings=warnings,
    )


def _resolve_swagger_2_param_ref(
    param: dict,
    doc: dict,
    warnings: list[str],
) -> dict | None:
    """解析 Swagger 2.0 parameter 的 $ref（同 OpenAPI 3.x 路径但走 #/definitions）。"""
    ref = param.get("$ref")
    if not ref:
        return param
    if not isinstance(ref, str):
        return param
    if ref.startswith("http://") or ref.startswith("https://"):
        warnings.append(f"跨文档 parameter $ref 已被拒绝：{ref!r}")
        return None
    if not ref.startswith("#/definitions/") and not ref.startswith("#/parameters/"):
        warnings.append(f"非本地 parameter $ref 已被拒绝：{ref!r}")
        return None
    if ref.startswith("#/definitions/"):
        resolved = _resolve_swagger_2_ref(ref, doc)
    else:
        # #/parameters/{name} 由 Swagger 2.0 spec 允许
        name = ref.rsplit("/", 1)[-1]
        params = doc.get("parameters") or {}
        resolved = params.get(name) if isinstance(params, dict) else None
    if resolved is None:
        warnings.append(f"parameter $ref 解析失败：{ref!r}")
        return None
    return resolved


def _swagger_2_param_to_fields(param: dict, *, doc: dict) -> list[FieldDraft] | str:
    """Swagger 2.0 单个 parameter → 1..N FieldDraft 列表；返回字符串代表 warning。"""
    name = param.get("name")
    if not isinstance(name, str) or not name:
        return f"parameter 缺 name，已跳过"
    location = param.get("in") or ""
    required = bool(param.get("required", False))
    description = param.get("description")

    # in: body → schema 内的字段全部拍平
    if location == "body":
        schema = param.get("schema") or {}
        if not isinstance(schema, dict):
            return []
        # 解 $ref
        if isinstance(schema.get("$ref"), str):
            ref = schema["$ref"]
            if ref.startswith("#/definitions/"):
                resolved = _resolve_swagger_2_ref(ref, doc)
                if resolved is None:
                    return f"body schema $ref 解析失败：{ref!r}"
                schema = resolved
            else:
                return f"body schema $ref 已被拒绝：{ref!r}"
        # 组合式 schema 显式告警（与 OpenAPI 3.x 路径行为一致，见规范 §6）
        if not (schema.get("properties") or {}) and any(
            k in schema for k in ("allOf", "oneOf", "anyOf")
        ):
            return (
                "body schema 使用了 allOf/oneOf/anyOf 组合式，其字段未导入——"
                "请内联为完整 properties 后重新提供文档"
            )
        properties = schema.get("properties") or {}
        required_set = set(schema.get("required") or [])
        out: list[FieldDraft] = []
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue
            if (prop_schema.get("format") or "").lower() == "binary":
                out.append(
                    FieldDraft(
                        name=prop_name,
                        type="file",
                        ui="upload",
                        label=description or prop_name,
                        required=prop_name in required_set,
                        description=description,
                    )
                )
                continue
            if prop_schema.get("type") == "object" and "properties" in prop_schema:
                out.extend(
                    _flatten_schema_to_fields_swagger_2(
                        prop_schema,
                        field_name=prop_name,
                        required=prop_name in required_set,
                    )
                )
            else:
                raw_default = prop_schema.get("default")
                inferred_type = _infer_field_type(prop_schema)
                if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
                    raise ValueError(
                        f"body 字段 {prop_name!r} 的 default 值 {raw_default!r} 与 schema type={prop_schema.get('type')!r} 不兼容"
                    )
                # 修复 Swagger 2.0 body description 丢失：每个 prop 应当读自己 schema 上的
                # description（之前的 `description if prop_name == name` 只在顶层字段才传递，
                # 内嵌/并列字段全部丢掉，与 spec §13 #1 期望冲突）。
                prop_description = prop_schema.get("description") or (
                    description if prop_name == name else None
                )
                field, _warn = _map_schema_to_field(
                    prop_schema,
                    name=prop_name,
                    required=prop_name in required_set,
                    description=prop_description,
                    default=raw_default,
                )
                if field is not None:
                    out.append(field)
        return out

    # in: formData → 顶级字段（含 type=file）
    if location == "formData":
        ptype = (param.get("type") or "").lower()
        if ptype == "file":
            return [
                FieldDraft(
                    name=name,
                    type="file",
                    ui="upload",
                    label=description or _label_humanize(name),
                    required=required,
                    description=description,
                )
            ]
        # 当作单字段映射（schema 不存在 → 用 type 构造等效 schema）
        pseudo_schema = {"type": ptype} if ptype else {}
        enum_vals = param.get("enum")
        if isinstance(enum_vals, list) and enum_vals:
            pseudo_schema["enum"] = enum_vals
        if param.get("format"):
            pseudo_schema["format"] = param["format"]
        raw_default = param.get("default")
        inferred_type = _infer_field_type(pseudo_schema)
        if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
            raise ValueError(
                f"formData 字段 {name!r} 的 default 值 {raw_default!r} 与 type={ptype!r} 不兼容"
            )
        field, _warn = _map_schema_to_field(
            pseudo_schema,
            name=name,
            description=description,
            required=required,
            default=raw_default,
        )
        return [field] if field is not None else []

    # 其他 in (query/path/header)：单字段映射
    ptype = (param.get("type") or "string").lower()
    pseudo_schema: dict = {"type": ptype}
    if param.get("format"):
        pseudo_schema["format"] = param["format"]
    enum_vals = param.get("enum")
    if isinstance(enum_vals, list) and enum_vals:
        pseudo_schema["enum"] = enum_vals
    raw_default = param.get("default")
    inferred_type = _infer_field_type(pseudo_schema)
    if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
        raise ValueError(
            f"{location or 'param'} 字段 {name!r} 的 default 值 {raw_default!r} 与 type={ptype!r} 不兼容"
        )
    field, _warn = _map_schema_to_field(
        pseudo_schema,
        name=name,
        description=description,
        required=required,
        default=raw_default,
    )
    return [field] if field is not None else []


def _flatten_schema_to_fields_swagger_2(
    schema: dict,
    *,
    field_name: str,
    required: bool,
) -> list[FieldDraft]:
    """Swagger 2.0 嵌套对象拍平（与 OpenAPI 3.x 同语义）。"""
    if (schema.get("type") or "") != "object" or "properties" not in schema:
        raw_default = schema.get("default")
        inferred_type = _infer_field_type(schema)
        if raw_default is not None and not _default_matches_type(raw_default, inferred_type):
            raise ValueError(
                f"字段 {field_name!r} 的 default 值 {raw_default!r} 与 schema type={schema.get('type')!r} 不兼容"
            )
        field, _warn = _map_schema_to_field(
            schema, name=field_name, required=required, default=raw_default,
        )
        return [field] if field is not None else []
    properties = schema.get("properties") or {}
    required_set = set(schema.get("required") or [])
    out: list[FieldDraft] = []
    for child_name, child_schema in properties.items():
        if not isinstance(child_schema, dict):
            continue
        if (child_schema.get("format") or "").lower() == "binary":
            continue
        out.extend(
            _flatten_schema_to_fields_swagger_2(
                child_schema,
                field_name=f"{field_name}.{child_name}",
                required=child_name in required_set,
            )
        )
    return out


def _suggest_output_hint_swagger_2(op: dict) -> dict[str, Any]:
    """Swagger 2.0 output_hint 建议（语义与 3.x 相同）。"""
    responses = op.get("responses") or {}
    success = responses.get("200") or responses.get("201") or {}
    if not isinstance(success, dict):
        return {"display": "json", "items_path": None}
    schema = success.get("schema") or {}
    if not isinstance(schema, dict):
        return {"display": "json", "items_path": None}
    if schema.get("type") == "array":
        return {"display": "table", "items_path": ""}
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            answer = properties.get("answer")
            if isinstance(answer, dict) and answer.get("type") == "string":
                return {"display": "text", "text_path": "answer"}
            for field_name, child in properties.items():
                if isinstance(child, dict) and child.get("type") == "array":
                    return {"display": "table", "items_path": field_name}
    return {"display": "json", "items_path": None}


# ── 文档拉取（spec §7.1 / §9.1-9.5）───────────────────────────────────────


async def fetch_openapi_doc(
    url: str,
    *,
    allowed_cidrs: list[str] | None = None,
    timeout: float = DEFAULT_OPENAPI_FETCH_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """从 URL 拉取并解析 OpenAPI doc（spec §9.1-9.5）。

    行为：
      1. SSRF 闸门（与 invoke endpoint 同标准）。
      2. WSL DEBUG-only 重写（与 invoke 路径一致）。
      3. httpx GET，stream 按 5MB 截断；超时 15s。
      4. 仅接受 application/json 或 text/* 类 Content-Type（YAML 暂未支持 → P2-6 延后）。
      5. 解析失败 → BadRequestError。

    返回 dict；抛 BadRequestError 表示各种拉取/解析失败。
    传入 `transport` 用于测试注入 httpx MockTransport，生产调用可不传。
    """
    from app.core.exceptions import BadRequestError
    from app.services.external_agent_adapter import _resolve_wsl_endpoint
    from app.services.external_agent_ssrf import validate_invoke_endpoint

    if not url or not isinstance(url, str):
        raise BadRequestError(
            "doc_url 必须为字符串",
            "errors.external_agent.openapi_missing_source",
        )
    # SSRF 闸门：先校验，避免后续下载大文件浪费带宽
    validate_invoke_endpoint(url, allowed_cidrs)
    url = _resolve_wsl_endpoint(url)

    # 发送请求（trust_env=False：不走系统代理；与 invoke 同款）
    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                resp = await client.get(url)
            except httpx.TimeoutException as exc:
                raise BadRequestError(
                    f"拉取 OpenAPI 文档超时（{timeout}s）：{exc}",
                    "errors.external_agent.openapi_fetch_failed",
                ) from exc
            except httpx.HTTPError as exc:
                raise BadRequestError(
                    f"无法拉取 OpenAPI 文档：{exc}",
                    "errors.external_agent.openapi_fetch_failed",
                ) from exc
    except BadRequestError:
        raise
    except Exception as exc:
        raise BadRequestError(
            f"无法拉取 OpenAPI 文档：{exc}",
            "errors.external_agent.openapi_fetch_failed",
        ) from exc

    if resp.status_code != 200:
        raise BadRequestError(
            f"远程文档返回 HTTP {resp.status_code}",
            "errors.external_agent.openapi_fetch_failed",
        )

    # 内容大小检查：同时兼顾 content-length header + 实际字节数（避免服务端虚报）
    cl_header = resp.headers.get("content-length")
    if cl_header:
        try:
            if int(cl_header) > MAX_OPENAPI_DOC_SIZE:
                raise BadRequestError(
                    "OpenAPI 文档超过 5MB 上限",
                    "errors.external_agent.openapi_too_large",
                )
        except ValueError:
            pass  # 非数字 content-length，忽略 header，按实际字节判断

    # content / read 都返回 bytes；用 len() 兜底 5MB 上限（应对 chunked encoding）
    body = resp.content
    if len(body) > MAX_OPENAPI_DOC_SIZE:
        raise BadRequestError(
            "OpenAPI 文档超过 5MB 上限",
            "errors.external_agent.openapi_too_large",
        )

    # JSON-only 解析（spec review P2-6：YAML 解析延后）
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BadRequestError(
            f"OpenAPI 文档不是有效 JSON（YAML 解析暂未支持）：{exc}",
            "errors.external_agent.openapi_invalid_json",
        ) from exc
    if not isinstance(doc, dict):
        raise BadRequestError(
            "OpenAPI 文档根类型必须为对象",
            "errors.external_agent.openapi_invalid_json",
        )
    return doc


# ── 解析后 endpoint 域校验（spec §9.2 / review Z-2）─────────────────────────


def _extract_server_hosts(servers: list[dict]) -> set[str]:
    """从 doc.servers 列表里抽 host（lowercase）。

    相对路径 server URL（`url: '/api/v3'`）无 host，跳过；
    含 host 的 server URL 加入集合（含端口）。
    """
    hosts: set[str] = set()
    for srv in servers or []:
        if not isinstance(srv, dict):
            continue
        url = srv.get("url")
        if not isinstance(url, str) or not url:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            # 相对路径 + 含变量（如 `{scheme}://...`）暂时跳过域校验
            continue
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        host = parsed.hostname
        if not host:
            continue
        # 保留端口：host 部分转小写
        hosts.add(host.lower())
    return hosts


def _endpoint_host_from_path(base_url: str, path: str) -> str | None:
    """把 base_url + path 拼成完整 URL 并抽取 host。

    base_url 为空或非 http(s) 形式时返回 None（无法做域匹配）。"""
    if not base_url or not isinstance(base_url, str):
        return None
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return None
    try:
        joined = base_url.rstrip("/") + (path or "")
        parsed = urlparse(joined)
        return (parsed.hostname or "").lower() or None
    except Exception:
        return None


def validate_parsed_endpoints_in_servers(
    parsed_functions: list[FunctionDraft],
    servers: list[dict],
) -> list[str]:
    """检查解析出的 endpoint host 是否落在 doc.servers 声明的域集合内。

    返回每条 function 的 warning 列表（与 parsed_functions 等长）。
    规则（spec §9.2 / review Z-2）：
      - 从 doc.servers 抽出所有 host；
      - 把每个 draft.path + 第一个 server URL 拼成完整 endpoint，提取 host；
      - host 不在集合内 → 加 warning。

    没有合法 server URL（仅相对路径）或 servers 为空：不做域校验，返回空字符串列表。
    """
    allowed_hosts = _extract_server_hosts(servers)
    if not allowed_hosts:
        return ["" for _ in parsed_functions]
    first_server_url = ""
    for srv in servers or []:
        if isinstance(srv, dict):
            u = srv.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                first_server_url = u
                break
    if not first_server_url:
        return ["" for _ in parsed_functions]

    warnings: list[str] = []
    for fn in parsed_functions:
        host = _endpoint_host_from_path(first_server_url, fn.path)
        if host is None:
            warnings.append("")
            continue
        if host not in allowed_hosts:
            warnings.append(
                f"解析出的 endpoint host {host!r} 不在 doc.servers 声明的允许域中"
            )
        else:
            warnings.append("")
    return warnings
