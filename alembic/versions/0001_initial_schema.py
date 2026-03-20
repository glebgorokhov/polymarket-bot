"""Initial schema — all tables

Revision ID: 0001
Revises: 
Create Date: 2024-01-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all tables from scratch."""

    # traders
    op.create_table(
        "traders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="watching"),
        sa.Column("category_strengths", JSONB(), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("monthly_pnl_history", JSONB(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("address"),
    )
    op.create_index("ix_traders_address", "traders", ["address"])
    op.create_index("ix_traders_status", "traders", ["status"])

    # trader_snapshots
    op.create_table(
        "trader_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("pnl_30d", sa.Float(), nullable=True),
        sa.Column("sharpe_ratio", sa.Float(), nullable=True),
        sa.Column("consistency_score", sa.Float(), nullable=True),
        sa.Column("trade_count_30d", sa.Integer(), nullable=True),
        sa.Column("categories_json", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["trader_id"], ["traders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trader_snapshots_trader_id", "trader_snapshots", ["trader_id"])

    # strategies
    op.create_table(
        "strategies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_strategies_slug", "strategies", ["slug"])

    # signals
    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader_id", sa.BigInteger(), nullable=False),
        sa.Column("market_condition_id", sa.String(128), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("size_usd", sa.Float(), nullable=False),
        sa.Column("raw_trade_id", sa.String(128), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("action_taken", sa.String(16), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("strategy_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["trader_id"], ["traders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_trader_id", "signals", ["trader_id"])
    op.create_index("ix_signals_market_condition_id", "signals", ["market_condition_id"])

    # positions
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market_condition_id", sa.String(128), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=False),
        sa.Column("market_name", sa.String(512), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("size_usd", sa.Float(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("strategy_id", sa.BigInteger(), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(128), nullable=True),
        sa.Column("entry_cost", sa.Float(), nullable=True),
        sa.Column("exit_value", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("pnl_pct", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_market_condition_id", "positions", ["market_condition_id"])
    op.create_index("ix_positions_status", "positions", ["status"])

    # executions
    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.String(128), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("size", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executions_position_id", "executions", ["position_id"])

    # strategy_results
    op.create_table(
        "strategy_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trades_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_deployed", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_strategy_results_strategy_id", "strategy_results", ["strategy_id"])

    # settings
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # reports
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_text", sa.Text(), nullable=False),
        sa.Column("metrics_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # markets_cache
    op.create_table(
        "markets_cache",
        sa.Column("condition_id", sa.String(128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("condition_id"),
    )

    # events_log
    op.create_table(
        "events_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("data_json", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_log_event_type", "events_log", ["event_type"])


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table("events_log")
    op.drop_table("markets_cache")
    op.drop_table("reports")
    op.drop_table("settings")
    op.drop_table("strategy_results")
    op.drop_table("executions")
    op.drop_table("positions")
    op.drop_table("signals")
    op.drop_table("strategies")
    op.drop_table("trader_snapshots")
    op.drop_table("traders")
