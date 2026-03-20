"""add is_shadow to positions

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-20

"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('positions', sa.Column('is_shadow', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('positions', 'is_shadow')
