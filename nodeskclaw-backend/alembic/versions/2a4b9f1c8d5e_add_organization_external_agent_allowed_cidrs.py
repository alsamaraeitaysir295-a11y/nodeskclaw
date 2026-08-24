"""add_organization_external_agent_allowed_cidrs

Revision ID: 2a4b9f1c8d5e
Revises: 1f16ebe82ade
Create Date: 2026-08-19 12:00:00.000000

Phase 1 任务 #5 / 方案 §9.2：
Organization 新增外部智能体插件 SSRF 白名单字段（JSONB 数组，CIDR 列表）。
默认 null → SSRF 闸门拒绝所有私网 / 回环；org admin 可通过
PUT /orgs/{id}/external-agent-allowed-cidrs 设置。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2a4b9f1c8d5e'
down_revision: Union[str, Sequence[str], None] = '1f16ebe82ade'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'organizations',
        sa.Column(
            'external_agent_allowed_cidrs',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'external_agent_allowed_cidrs')
