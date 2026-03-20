"""add signals.market_name and signals.strategies_triggered

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('market_name', sa.String(512), nullable=True))
    op.add_column('signals', sa.Column('strategies_triggered', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('signals', 'strategies_triggered')
    op.drop_column('signals', 'market_name')
