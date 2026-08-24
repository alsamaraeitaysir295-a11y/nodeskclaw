"""外部 Agent tool 型插件的调用历史模型。

用户侧「调用历史」功能的数据源（区别于审计表 operation_audit_logs：
审计面向管理员聚合统计，本表面向普通用户回看自己的调用与结果）。

设计要点：
- 每次 invoke（成功 / 上游失败 / 不可达）落一条记录；本地参数校验失败（422）
  未发起外部调用，不记录。
- 仅按 (agent_id, user_id) 查询，用户只能看到自己的记录（无跨用户泄露面）。
- `result_data` 存完整 invoke 响应（success/data/display/items_path...）供前端
  PluginResult 重放渲染；超 50KB 截断为 truncation 提示，避免撑爆 DB。
- `params_summary` 存参数 JSON 摘要（仅用户本人可见，文件字段为 storage_key）。
- 软删除继承 BaseModel.deleted_at（查询一律过滤），FK 跟随 agent 级联。
"""
from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ExternalAgentInvocation(BaseModel):
    """外部 Agent tool 型插件的单次调用记录（用户可见的调用历史）。"""

    __tablename__ = "external_agent_invocations"
    # 历史列表按 (agent_id, user_id, created_at desc) 查询，建组合索引
    __table_args__ = (
        Index(
            "ix_external_agent_invocations_agent_user_created",
            "agent_id", "user_id", "created_at",
        ),
    )

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 调用的 function（Phase 1 兼容代理路径也指向 default function）
    function_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    function_name: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 参数 JSON 摘要（超长截断），仅用户本人历史展示用
    params_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 完整 invoke 响应（50KB 截断），前端点击历史项重放渲染
    result_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
