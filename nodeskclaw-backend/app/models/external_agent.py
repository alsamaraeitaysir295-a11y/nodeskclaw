"""ExternalAgent 模型：表示运行在外部服务器上的专用 AI Agent 服务。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ExternalAgent(BaseModel):
    __tablename__ = "external_agents"

    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 插件分类：chat（A类·对话型，走会话+SSE）| tool（B类·信息化系统型，走表单+invoke）
    type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chat", server_default="chat"
    )
    # 外部 Agent 服务的基础 URL（如 http://agent.example.com:8000）。tool 型不使用此字段。
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    # AES-256-GCM 加密后的 API Key，base64(nonce + ciphertext)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 通信协议（仅 chat 型）：openai_compatible | custom | nap | rag_standard
    protocol: Mapped[str] = mapped_column(
        String(32), nullable=False, default="openai_compatible", server_default="openai_compatible"
    )
    # 会话由谁管理（仅 rag_standard 允许 "external"）：platform（默认）| external
    session_managed_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # JSON 数组，能力标签（如 ["代码审查", "SQL生成"]），供卡片展示
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 卡片装饰
    icon_emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)
    theme_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 由 /sync 端点写入，标记外部服务是否可达
    is_reachable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # tool 型：外部接口调用配置（endpoint/method/auth/timeout_seconds/pass_mode/output_hint）
    # DEPRECATED（Phase 2 起）：新读写路径走 `external_agent_functions.invoke_config`，
    # 旧列仅作数据回滚保险（迁移时按 `type='tool'` 行回填 sort_order=0 的 default function
    # 到 external_agent_functions 表）。新代码不应再向本列写入；读取仅在兼容代理 /
    # 旧 invoke 端点未识别对应 function 时回退。
    invoke_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # tool 型：表单字段定义（input_schema，见方案 §4）
    # DEPRECATED（Phase 2 起）：新读写路径走 `external_agent_functions.input_schema`，
    # 旧列保留语义同 `invoke_config`。
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 生命周期状态：draft（试调未通过，仅创建者可见）| active | disabled（手动停用）
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # Manifest 版本号，input_schema/invoke_config 变更时 +1，前端表单据此使缓存失效
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 最近一次试调/健康探测结果：{ok, http_code, latency_ms, error, at}
    last_probe: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

