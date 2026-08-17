"""add review status to instance template

Revision ID: 63e9866b38a5
Revises: d818fc8b3cd2
Create Date: 2026-08-17 10:22:37.042684

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63e9866b38a5'
down_revision: Union[str, Sequence[str], None] = 'd818fc8b3cd2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('instance_templates', sa.Column('review_status', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('instance_templates', 'review_status')
