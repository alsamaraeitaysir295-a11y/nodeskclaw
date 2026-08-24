"""ExternalAgent 的 Pydantic 请求/响应 Schema。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel as PydanticBase, create_model, field_validator, model_validator

logger = logging.getLogger(__name__)


class ExternalAgentCreate(PydanticBase):
    name: str
    # endpoint 仅 chat 型必填；tool 型填在 invoke_config.endpoint 下
    # （spec §4：tool 型不带顶层 endpoint）
    endpoint: str | None = None
    api_key: str | None = None
    protocol: Literal["openai_compatible", "custom", "nap", "rag_standard"] | None = None
    description: str | None = None
    capabilities: list[str] = []
    icon_emoji: str | None = None
    theme_color: str | None = None

    # ── 插件化扩展字段（Phase 1 §4 Manifest） ──
    type: Literal["chat", "tool"] | None = None
    session_managed_by: Literal["external"] | None = None
    invoke_config: ManifestInvokeConfig | None = None
    input_schema: ManifestInputSchema | None = None
    output_hint: ManifestOutputHint | None = None
    status: Literal["draft", "active", "disabled"] | None = None


class ExternalAgentUpdate(PydanticBase):
    name: str | None = None
    endpoint: str | None = None
    api_key: str | None = None
    protocol: Literal["openai_compatible", "custom", "nap", "rag_standard"] | None = None
    description: str | None = None
    capabilities: list[str] | None = None
    icon_emoji: str | None = None
    theme_color: str | None = None

    type: Literal["chat", "tool"] | None = None
    session_managed_by: Literal["external"] | None = None
    invoke_config: ManifestInvokeConfig | None = None
    input_schema: ManifestInputSchema | None = None
    output_hint: ManifestOutputHint | None = None
    status: Literal["draft", "active", "disabled"] | None = None


class ExternalAgentResponse(PydanticBase):
    id: str
    org_id: str
    name: str
    description: str | None
    endpoint: str
    protocol: str
    capabilities: list[str]
    icon_emoji: str | None
    theme_color: str | None
    is_reachable: bool
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # 插件化扩展字段（Phase 1 §4）
    type: str = "chat"
    session_managed_by: str | None = None
    invoke_config: dict | None = None
    input_schema: dict | None = None
    # output_hint 在 DB 上为 invoke_config 的嵌套字段（_output_hint sub-key），
    # 与外部调用配置同属一组运行时参数；这里拆出来返回给用户方便读写。
    output_hint: dict | None = None
    status: str = "active"
    version: int = 1
    last_probe: dict | None = None
    # Phase 2 §8.4：tool 型插件下的 function 数量（未软删）。
    # 仅在 list 端点用 group-by subquery 填充；其它端点（get/create/update）默认 0，
    # 由前端按 `agent.type === 'tool'` 自行决定是否展示。
    function_count: int = 0

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _lift_output_hint(cls, data: Any) -> Any:
        """从 invoke_config._output_hint 把它"抬"成 sibling 字段，便于前端消费。"""
        if isinstance(data, dict) and data.get("invoke_config") and "_output_hint" in data["invoke_config"]:
            data = {**data}
            data["output_hint"] = data["invoke_config"].pop("_output_hint")
        return data

    @field_validator("capabilities", mode="before")
    @classmethod
    def parse_capabilities(cls, v: Any) -> list[str]:
        """数据库存 JSON 字符串，Pydantic 验证前自动解析为列表。"""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                return []
        return v or []


# ── 附件 Schema ───────────────────────────────────────────────────────────────

class AttachmentItem(PydanticBase):
    """附件元数据（DB 存储格式，不含 URL）。"""

    name: str
    size: int
    content_type: str
    storage_key: str


class AttachmentItemWithUrl(AttachmentItem):
    """附件元数据 + 预签名 URL（仅用于 API 响应，不持久化）。"""

    url: str


# ── 会话 Schema ───────────────────────────────────────────────────────────────

class ChatSessionResponse(PydanticBase):
    id: str
    agent_id: str
    user_id: str
    org_id: str
    title: str | None
    external_session_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── 消息 Schema ───────────────────────────────────────────────────────────────

class MessageResponse(PydanticBase):
    id: str
    session_id: str
    role: str
    content: str
    thinking: str | None = None
    attachments: list[AttachmentItemWithUrl] | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── 聊天请求 Schema ────────────────────────────────────────────────────────────

class ChatRequest(PydanticBase):
    """新版聊天请求（后端从 DB 加载历史，前端只发当前消息）。"""

    message: str
    session_id: str
    attachments: list[AttachmentItem] | None = None


# ── Plugin Manifest Schema（外部智能体插件化接入 Phase 1 §4）───────────────────

class ManifestFieldSchema(PydanticBase):
    """tool 型 input_schema.fields 单个字段定义。"""

    type: Literal["string", "number", "boolean", "object", "file"]
    ui: Literal["input", "textarea", "select", "number", "switch", "date", "daterange", "upload"]
    label: str
    required: bool = False
    description: str | None = None      # 字段作用，用户端渲染为字段下方提示文字
    default: Any | None = None          # 默认参数，用户端预填 + 服务端兜底
    options: list[str] | None = None
    accept: list[str] | None = None
    max_mb: int | None = None

    @model_validator(mode="after")
    def _validate(self) -> ManifestFieldSchema:
        if self.ui == "select" and not self.options:
            raise ValueError("select 控件必须提供 options")
        if self.max_mb is not None and self.max_mb > 50:
            raise ValueError("max_mb 不能超过 50")
        # file 字段不允许 default（上传文件每次都需用户重新上传）
        if self.type == "file" and self.default is not None:
            raise ValueError("type=file 不允许提供 default（文件需每次重新上传）")
        # default 类型必须与 type 匹配
        if self.default is not None and not _default_matches_type(self.default, self.type):
            raise ValueError(
                f"default 值类型与字段 type={self.type} 不匹配（spec §4.1）"
            )
        # default + required 同时设置不阻断，但通过 logger 留痕——
        # 用户可见的警告路径在 /plugins/validate 的 warnings 字段。
        if self.default is not None and self.required:
            logger.warning(
                "ManifestFieldSchema '%s' 同时设置 default 与 required，按用户可覆盖处理",
                self.label or "?",
            )
        return self


class ManifestInputSchema(PydanticBase):
    """tool 型表单定义：order 决定渲染顺序，fields 为字段名 → 定义的映射。"""

    order: list[str]
    fields: dict[str, ManifestFieldSchema]

    @model_validator(mode="after")
    def _validate_order(self) -> ManifestInputSchema:
        missing = [k for k in self.order if k not in self.fields]
        if missing:
            raise ValueError(f"order 中的字段未在 fields 中定义: {missing}")
        return self


class ManifestOutputHint(PydanticBase):
    """tool 型结果展示提示。

    display=text 时按 text_path 取字符串字段（如 RAG 的 answer），
    前端渲染为 Markdown 文档（自动剥除推理模型输出的 think 前缀）。
    """

    display: Literal["table", "json", "text"] = "json"
    items_path: str | None = None
    text_path: str | None = None
    primary_key: str | None = None


class ManifestInvokeAuth(PydanticBase):
    type: Literal["none", "bearer", "api_key_header"] = "none"
    header_name: str | None = None
    token: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> ManifestInvokeAuth:
        if self.type == "api_key_header" and not self.header_name:
            raise ValueError("api_key_header 鉴权类型必须提供 header_name")
        return self


class ManifestInvokeConfig(PydanticBase):
    """tool 型外部接口调用配置。

    extra="allow"：invoke_config 同时是存储载体，`_output_hint` 子键（review P1-1
    的 output_hint 存放位置）不在显式字段里，校验/回写（model_dump）时必须透传，
    否则 PATCH 功能配置会把已有的 output_hint 静默剥掉。
    """

    model_config = {"extra": "allow"}

    endpoint: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    auth: ManifestInvokeAuth = ManifestInvokeAuth()
    timeout_seconds: int = 30
    pass_mode: Literal["multipart", "url_ref"] = "multipart"

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("invoke.endpoint 必须是 http(s) URL")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if v > 120:
            raise ValueError("timeout_seconds 不能超过 120")
        return v


class PluginManifest(PydanticBase):
    """插件清单：管理端向导第 3 步 /plugins/validate 与创建接口共用的校验模型。

    daterange 类型字段的运行时取值格式定死为 {"start": ISO-8601, "end": ISO-8601}，
    该约束在 §4 中要求前端控件、后端校验（task #4 动态模型）、外部传参三处一致，
    Manifest 本身（即字段定义）不需要在这里额外校验。
    """

    name: str
    description: str | None = None
    icon_emoji: str | None = None
    theme_color: str | None = None
    type: Literal["chat", "tool"]

    # chat 型
    protocol: Literal["openai_compatible", "custom", "nap", "rag_standard"] | None = None
    endpoint: str | None = None
    api_key: str | None = None
    session_managed_by: Literal["external"] | None = None

    # tool 型
    invoke: ManifestInvokeConfig | None = None
    input_schema: ManifestInputSchema | None = None
    output_hint: ManifestOutputHint | None = None

    @model_validator(mode="after")
    def _validate_type_specific(self) -> PluginManifest:
        if self.type == "chat":
            if not self.protocol:
                raise ValueError("chat 型必须提供 protocol")
            if not self.endpoint:
                raise ValueError("chat 型必须提供 endpoint")
            if self.session_managed_by == "external" and self.protocol != "rag_standard":
                raise ValueError("session_managed_by=external 仅 rag_standard 协议允许")
        elif self.type == "tool":
            if not self.invoke:
                raise ValueError("tool 型必须提供 invoke 配置")
        return self


_MANIFEST_FIELD_PY_TYPE: dict[str, type] = {
    "string": str,
    "number": float,
    "boolean": bool,
    "object": dict,
    # file 字段在动态校验模型中以 file_id（由 /{id}/files 上传后返回）表示
    "file": str,
}


def _default_matches_type(default: Any, field_type: str) -> bool:
    """校验 default 值的运行时类型是否与字段声明的 type 匹配。

    规则（spec §4.1）：
    - None 永远视为可选类型允许（允许 default=None 即不预填）
    - string -> str（但拒绝 bool，因为 bool 是 str 的子类但语义不同）
    - number -> int / float（**严格拒绝 bool**，因 Python bool 是 int 子类）
    - boolean -> bool（严格）
    - object -> dict / list（JSON 容器即可）
    - file -> 不允许 default（上游校验已拦截；此处仅作 False 兜底）
    """
    if default is None:
        return True
    if field_type == "string":
        return isinstance(default, str) and not isinstance(default, bool)
    if field_type == "number":
        # 严格拒绝 bool，避免 True/False 误当 1/0 通过 number 校验
        if isinstance(default, bool):
            return False
        return isinstance(default, (int, float))
    if field_type == "boolean":
        return isinstance(default, bool)
    if field_type == "object":
        return isinstance(default, (dict, list))
    if field_type == "file":
        return False
    return False


def build_dynamic_input_model(input_schema: ManifestInputSchema) -> type[PydanticBase]:
    """按 input_schema.fields 用 pydantic create_model 动态构建入参校验模型。

    供 tool 型 POST /{id}/invoke（task #4）按用户提交参数做字段级校验；不引入
    jsonschema 依赖，统一走 pydantic 动态模型方案（见方案 §4 字段规范）。

    Phase 2 §4.1 增量：可选字段的 default 写入动态模型的默认值，使 API 客户端
    未传该字段时服务端兜底填充（而非仅靠前端预填）。
    """
    field_defs: dict[str, Any] = {}
    for name, field in input_schema.fields.items():
        py_type = _MANIFEST_FIELD_PY_TYPE[field.type]
        if field.required:
            field_defs[name] = (py_type, ...)
        else:
            # default 可能为 None（未设置默认）或非 None（用户设置的兜底值）；
            # pydantic create_model 第二个位置参数即为 Field 的 default 值。
            field_defs[name] = (py_type | None, field.default)
    return create_model("DynamicInputModel", **field_defs)


class PluginConnectivityResult(PydanticBase):
    ok: bool
    http_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    skipped_invoke: bool = False


class PluginValidateResponse(PydanticBase):
    """POST /plugins/validate 响应体。"""

    schema_ok: bool
    schema_errors: list[str] | None = None
    connectivity: PluginConnectivityResult | None = None
    warnings: list[str] = []


# ── Function CRUD（spec §7.2）───────────────────────────────────────────────


class ExternalAgentFunctionCreate(PydanticBase):
    """手动加功能的请求体（走 PluginManifest tool 分支的字段子集）。"""

    name: str  # 英文标识符
    summary: str | None = None
    invoke_config: ManifestInvokeConfig
    input_schema: ManifestInputSchema | None = None
    output_hint: ManifestOutputHint | None = None
    status: Literal["draft", "active", "disabled"] | None = None
    sort_order: int | None = None  # 若不填，service 层取"同 agent 下 max(sort_order)+1"


class ExternalAgentFunctionUpdate(PydanticBase):
    """编辑功能的请求体；None = 不变。"""

    name: str | None = None
    summary: str | None = None
    invoke_config: ManifestInvokeConfig | None = None
    input_schema: ManifestInputSchema | None = None
    output_hint: ManifestOutputHint | None = None
    status: Literal["draft", "active", "disabled"] | None = None
    sort_order: int | None = None


class ExternalAgentFunctionResponse(PydanticBase):
    """function 响应体。"""

    id: str
    agent_id: str
    name: str
    summary: str | None
    invoke_config: dict | None
    input_schema: dict | None
    # output_hint 仍走 invoke_config["_output_hint"] 抽出来（review P1-1）
    output_hint: dict | None = None
    status: str
    sort_order: int
    source: str
    origin_meta: dict | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _lift_output_hint(cls, data: Any) -> Any:
        """从 invoke_config._output_hint 抽出来当 sibling 字段（review P1-1）。

        兼容两种输入：
          1) dict：直接 pop _output_hint 改写 data["output_hint"]；
          2) ORM 对象（from_attributes=True）：copy 字典、把 invoke_config
             里的 _output_hint 提升到顶层（不影响原对象，避免污染缓存）。
        """
        invoke_cfg = None
        if isinstance(data, dict):
            invoke_cfg = data.get("invoke_config")
        else:
            invoke_cfg = getattr(data, "invoke_config", None)

        if isinstance(invoke_cfg, dict) and "_output_hint" in invoke_cfg:
            if isinstance(data, dict):
                data = {**data}
                data["output_hint"] = invoke_cfg.pop("_output_hint")
                data["invoke_config"] = invoke_cfg
            else:
                # ORM 对象：构造一个新 dict 喂给后续字段验证，
                # 不修改原对象的 invoke_config（避免副作用/缓存问题）。
                output_hint = invoke_cfg.get("_output_hint")
                data = {
                    "id": getattr(data, "id", None),
                    "agent_id": getattr(data, "agent_id", None),
                    "name": getattr(data, "name", None),
                    "summary": getattr(data, "summary", None),
                    "invoke_config": {k: v for k, v in invoke_cfg.items() if k != "_output_hint"},
                    "input_schema": getattr(data, "input_schema", None),
                    "output_hint": output_hint,
                    "status": getattr(data, "status", None),
                    "sort_order": getattr(data, "sort_order", None),
                    "source": getattr(data, "source", None),
                    "origin_meta": getattr(data, "origin_meta", None),
                    "version": getattr(data, "version", None),
                    "created_at": getattr(data, "created_at", None),
                    "updated_at": getattr(data, "updated_at", None),
                }
        return data

