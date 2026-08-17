"""split instance template slug uniqueness by scope

Revision ID: 675fc8765544
Revises: 63e9866b38a5
Create Date: 2026-08-17 11:52:38.055047

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '675fc8765544'
down_revision: Union[str, Sequence[str], None] = '63e9866b38a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('uq_instance_templates_slug_org_active', table_name='instance_templates', postgresql_where='deleted_at IS NULL')
    op.create_index(
        'uq_instance_templates_slug_org_active', 'instance_templates', ['slug', 'org_id'],
        unique=True, postgresql_where="deleted_at IS NULL AND visibility = 'org_private'",
    )
    op.create_index(
        'uq_instance_templates_slug_personal_active', 'instance_templates', ['slug', 'created_by'],
        unique=True, postgresql_where="deleted_at IS NULL AND visibility = 'personal'",
    )
    op.create_index(
        'uq_instance_templates_slug_public_active', 'instance_templates', ['slug'],
        unique=True, postgresql_where="deleted_at IS NULL AND visibility = 'public'",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_instance_templates_slug_public_active', table_name='instance_templates', postgresql_where="deleted_at IS NULL AND visibility = 'public'")
    op.drop_index('uq_instance_templates_slug_personal_active', table_name='instance_templates', postgresql_where="deleted_at IS NULL AND visibility = 'personal'")
    op.drop_index('uq_instance_templates_slug_org_active', table_name='instance_templates', postgresql_where="deleted_at IS NULL AND visibility = 'org_private'")
    op.create_index(
        'uq_instance_templates_slug_org_active', 'instance_templates', ['slug', 'org_id'],
        unique=True, postgresql_where='deleted_at IS NULL',
    )
