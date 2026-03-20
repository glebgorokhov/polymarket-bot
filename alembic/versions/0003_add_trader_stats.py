"""add trader stats: weekly_pnl_history, win_rate, avg_trades_per_week, avg_profit_per_trade

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('traders', sa.Column('weekly_pnl_history', JSONB(), nullable=True))
    op.add_column('traders', sa.Column('win_rate', sa.Float(), nullable=True))
    op.add_column('traders', sa.Column('avg_trades_per_week', sa.Float(), nullable=True))
    op.add_column('traders', sa.Column('avg_profit_per_trade', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('traders', 'avg_profit_per_trade')
    op.drop_column('traders', 'avg_trades_per_week')
    op.drop_column('traders', 'win_rate')
    op.drop_column('traders', 'weekly_pnl_history')
