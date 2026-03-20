"""Add trader_pnl_snapshots table for curve consistency tracking.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trader_pnl_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trader_address", sa.String(42), nullable=False, index=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leaderboard_pnl", sa.Float(), nullable=False),
        sa.Column("leaderboard_volume", sa.Float(), nullable=True),
        sa.Column("leaderboard_rank", sa.Integer(), nullable=True),
        sa.Column("open_position_value", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_trader_pnl_snapshots_address_time",
        "trader_pnl_snapshots",
        ["trader_address", "captured_at"],
    )


def downgrade() -> None:
    op.drop_table("trader_pnl_snapshots")
