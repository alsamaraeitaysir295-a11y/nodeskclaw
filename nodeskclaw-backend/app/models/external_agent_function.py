"""外部 Agent 下的单个功能（function-calling 工具 / 表单入口）。

1 插件 → N 功能（Phase 2 引入，替代 Phase 1 的"1 插件 1 invoke 接口"模型）。

设计要点：
- `invoke_config` / `input_schema` 列结构与 `ExternalAgent` 完全一致，
  复用 `ManifestInvokeConfig` / `ManifestInputSchema` 校验，tool_service
  无需感知存储位置变化。
- `name` 同插件内不可重（由 `(agent_id, name)` partial unique index 保证）。
- `sort_order` 同插件内排序，partial unique index `(agent_id, sort_order) WHERE deleted_at IS NULL`
  保证唯一；迁移的 `default` function 占 sort_order=0，新功能必须 >= 1（DB 层
  拒 insert sort_order=0 时会与 default 撞值）。
- 软删除继承 `BaseModel.deleted_at`，与 `ExternalAgent` 一致；FK 用
  `ondelete="CASCADE"` 应对硬删除场景（仓库内统一约定：物理删除时
  BaseModel 不会自动调用，需手动 drop_all 或 DDL DROP）。
"""
from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ExternalAgentFunction(BaseModel):
    """外部 Agent 下的单个功能（function-calling 工具 / 表单入口）。

    1 插件 → N 功能（Phase 2 引入，替代 Phase 1 的"1 插件 1 invoke 接口"模型）。
    """

    __tablename__ = "external_agent_functions"
    # 同插件内 name 与 sort_order 均唯一，且仅约束未软删除行（与 review P1-2 一致）。
    # 软删除行的 name/sort_order 允许被新功能复用，便于管理员"重命名"或"重新排序"。
    __table_args__ = (
        Index(
            "uq_external_agent_functions_agent_id_name",
            "agent_id", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_external_agent_functions_agent_id_sort_order",
            "agent_id", "sort_order",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("external_agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 功能名（英文标识，调用时使用）；同插件内不可重
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 功能说明，给用户看（也是 Phase 1.5 function-calling 的 tool description）
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 外部接口调用配置（结构与 ExternalAgent.invoke_config 一致）
    invoke_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 输入字段定义（结构与 ExternalAgent.input_schema 一致）
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 生命周期状态：draft | active | disabled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    # 同插件内的渲染顺序；默认 0（仅迁移的 default function 用），新功能 1+；
    # 与 agent_id 联合唯一（partial unique index，详见 alembic migration）。
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 来源：manual（手填）或 openapi_import（导入）
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    # OpenAPI 导入时的原始引用：{path, method, operationId, spec_version}
    origin_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Manifest 版本号，invoke_config/input_schema 变更时 +1（前端缓存缓存失效）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 来自 BaseModel：id / created_at / updated_at / deleted_at（软删除）
