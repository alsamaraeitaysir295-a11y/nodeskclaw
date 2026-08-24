"""add_external_agent_functions

Revision ID: 72c3f6496399
Revises: 2a4b9f1c8d5e
Create Date: 2026-08-20 09:48:17.504277

Phase 2 任务 #1：外部智能体插件多功能模型

新表 `external_agent_functions`：1 插件 → N 功能（function-calling 工具 / 表单入口）。
详见 `ee/docs/外部智能体二期方案.md` §5 与 review P1-1/P1-2/P2-8。

关键决策：
- `output_hint` 不开独立列，沿用 Phase 1 约定存 `invoke_config["_output_hint"]` 子键。
- `(agent_id, sort_order)` 用 partial unique index（仅约束非软删除行）保证唯一：
  迁移的 `default` function 占 sort_order=0；新功能必须 >= 1。
- 存量 `type='tool'` 且 `invoke_config IS NOT NULL` 的插件按 name='default' / sort_order=0
  迁移一条 function，status 继承插件 status。
- `external_agents` 表的 `invoke_config` / `input_schema` 列保留不删（加 deprecation 注释），
  读路径全部走 functions 表，旧列仅作回滚保险。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '72c3f6496399'
down_revision: Union[str, Sequence[str], None] = '2a4b9f1c8d5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── Step 1：建表 ──────────────────────────────────────────────────────────
    op.create_table(
        'external_agent_functions',
        sa.Column('agent_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('invoke_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('source', sa.String(length=16), server_default='manual', nullable=False),
        sa.Column('origin_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['external_agents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_external_agent_functions_deleted_at'),
        'external_agent_functions', ['deleted_at'], unique=False,
    )
    op.create_index(
        op.f('ix_external_agent_functions_agent_id'),
        'external_agent_functions', ['agent_id'], unique=False,
    )

    # ── Step 2：部分唯一索引 ───────────────────────────────────────────────────
    # `(agent_id, name)` / `(agent_id, sort_order)` 仅对未软删除的行唯一；
    # 软删除行的 name/sort_order 允许被新功能复用。
    # - name：同插件内功能名不可重（spec §5 唯一约束）。
    # - sort_order：迁移占 sort_order=0 后，任何新 function sort_order=0
    #   都会被 DB 拒绝（review P1-2）。
    op.create_index(
        'uq_external_agent_functions_agent_id_name',
        'external_agent_functions',
        ['agent_id', 'name'],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL'),
    )
    op.create_index(
        'uq_external_agent_functions_agent_id_sort_order',
        'external_agent_functions',
        ['agent_id', 'sort_order'],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL'),
    )

    # ── Step 3：存量 tool 插件数据迁移 ──────────────────────────────────────────
    # 仅 type='tool' 且 invoke_config 非空 的插件各生成一条 name='default' 的 function，
    # sort_order=0（partial unique index 允许，因为它只约束未软删除行；
    # 同 agent_id 下只会有此一条 sort_order=0 的存活行）。
    conn = op.get_bind()
    inserted = conn.execute(sa.text("""
        INSERT INTO external_agent_functions (
            id, agent_id, name, summary, invoke_config, input_schema,
            status, sort_order, source, origin_meta, version,
            created_at, updated_at, deleted_at
        )
        SELECT
            gen_random_uuid()::text,
            ea.id,
            'default',
            ea.description,
            ea.invoke_config,
            ea.input_schema,
            ea.status,
            0,
            'manual',
            NULL,
            ea.version,
            NOW(),
            NOW(),
            NULL
        FROM external_agents ea
        WHERE ea.type = 'tool'
          AND ea.invoke_config IS NOT NULL
          AND ea.deleted_at IS NULL
    """))
    migrated_count = inserted.rowcount or 0

    # ── Step 4：迁移后一致性自检（仅日志）───────────────────────────────────────
    # 每个 tool 型插件应该至少有 1 条未软删除 function。如果数量不匹配则告警。
    mismatch = conn.execute(sa.text("""
        SELECT COUNT(*) AS missing
        FROM external_agents ea
        WHERE ea.type = 'tool'
          AND ea.invoke_config IS NOT NULL
          AND ea.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM external_agent_functions f
              WHERE f.agent_id = ea.id AND f.deleted_at IS NULL
          )
    """)).scalar_one()
    if mismatch:
        # 使用 print 而非 logger：alembic 环境未配置 logging handler，
        # logger.warning 会沉默，运维容易忽略。
        print(
            f"[WARNING] add_external_agent_functions migration: "
            f"{mismatch} tool-type agents have no function row after backfill "
            f"(migrated={migrated_count})."
        )
    else:
        print(
            f"[INFO] add_external_agent_functions migration: backfilled "
            f"{migrated_count} default function rows from legacy type='tool' plugins."
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_external_agent_functions_agent_id_sort_order',
        table_name='external_agent_functions',
    )
    op.drop_index(
        'uq_external_agent_functions_agent_id_name',
        table_name='external_agent_functions',
    )
    op.drop_index(
        op.f('ix_external_agent_functions_agent_id'),
        table_name='external_agent_functions',
    )
    op.drop_index(
        op.f('ix_external_agent_functions_deleted_at'),
        table_name='external_agent_functions',
    )
    op.drop_table('external_agent_functions')
