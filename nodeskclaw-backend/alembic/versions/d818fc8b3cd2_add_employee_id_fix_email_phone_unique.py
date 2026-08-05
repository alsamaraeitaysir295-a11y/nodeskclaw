"""add_employee_id_fix_email_phone_unique

Revision ID: d818fc8b3cd2
Revises: b50c716fd16f
Create Date: 2026-08-05 16:00:03.182335

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd818fc8b3cd2'
down_revision: Union[str, Sequence[str], None] = 'b50c716fd16f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('employee_id', sa.String(length=8), nullable=True))
    op.drop_constraint(op.f('users_email_key'), 'users', type_='unique')
    op.drop_constraint(op.f('users_phone_key'), 'users', type_='unique')
    op.create_index('uq_users_email', 'users', ['email'], unique=True, postgresql_where=sa.text('deleted_at IS NULL AND email IS NOT NULL'))
    op.create_index('uq_users_employee_id', 'users', ['employee_id'], unique=True, postgresql_where=sa.text('deleted_at IS NULL AND employee_id IS NOT NULL'))
    op.create_index('uq_users_phone', 'users', ['phone'], unique=True, postgresql_where=sa.text('deleted_at IS NULL AND phone IS NOT NULL'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_users_phone', table_name='users', postgresql_where=sa.text('deleted_at IS NULL AND phone IS NOT NULL'))
    op.drop_index('uq_users_employee_id', table_name='users', postgresql_where=sa.text('deleted_at IS NULL AND employee_id IS NOT NULL'))
    op.drop_index('uq_users_email', table_name='users', postgresql_where=sa.text('deleted_at IS NULL AND email IS NOT NULL'))
    op.create_unique_constraint(op.f('users_phone_key'), 'users', ['phone'], postgresql_nulls_not_distinct=False)
    op.create_unique_constraint(op.f('users_email_key'), 'users', ['email'], postgresql_nulls_not_distinct=False)
    op.drop_column('users', 'employee_id')
