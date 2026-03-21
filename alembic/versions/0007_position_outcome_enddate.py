"""Add outcome and end_date to positions table.

Revision ID: 0007_position_outcome_enddate
Revises: 0006_trader_pnl_snapshots
Create Date: 2026-03-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("outcome", sa.String(128), nullable=True))
    op.add_column("positions", sa.Column("end_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("positions", sa.Column("trader_address", sa.String(42), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "outcome")
    op.drop_column("positions", "end_date")
    op.drop_column("positions", "trader_address")
