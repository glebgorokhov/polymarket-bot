"""add traders.is_pinned and traders.leaderboard_rank

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('traders', sa.Column('is_pinned', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('traders', sa.Column('leaderboard_rank', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('traders', 'leaderboard_rank')
    op.drop_column('traders', 'is_pinned')
