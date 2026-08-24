"""add_external_agent_plugin_fields

Revision ID: 1f16ebe82ade
Revises: 675fc8765544
Create Date: 2026-08-19 10:20:05.144101

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1f16ebe82ade'
down_revision: Union[str, Sequence[str], None] = '675fc8765544'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('external_agent_chat_sessions', sa.Column('external_session_id', sa.String(length=128), nullable=True))
    op.add_column('external_agents', sa.Column('type', sa.String(length=16), server_default='chat', nullable=False))
    op.add_column('external_agents', sa.Column('session_managed_by', sa.String(length=16), nullable=True))
    op.add_column('external_agents', sa.Column('invoke_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('external_agents', sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('external_agents', sa.Column('status', sa.String(length=16), server_default='active', nullable=False))
    op.add_column('external_agents', sa.Column('version', sa.Integer(), server_default='1', nullable=False))
    op.add_column('external_agents', sa.Column('last_probe', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('external_agents', 'last_probe')
    op.drop_column('external_agents', 'version')
    op.drop_column('external_agents', 'status')
    op.drop_column('external_agents', 'input_schema')
    op.drop_column('external_agents', 'invoke_config')
    op.drop_column('external_agents', 'session_managed_by')
    op.drop_column('external_agents', 'type')
    op.drop_column('external_agent_chat_sessions', 'external_session_id')
