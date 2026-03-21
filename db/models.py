"""
SQLAlchemy 2.0 declarative models for the Polymarket copytrading bot.
All models use the async-compatible ORM style.
"""

from datetime import datetime, date
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class Trader(Base):
    """Tracked Polymarket traders."""

    __tablename__ = "traders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="watching", nullable=False, index=True
    )  # active / inactive / watching
    category_strengths: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    monthly_pnl_history: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    weekly_pnl_history: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)          # fraction of weeks profitable
    avg_trades_per_week: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_profit_per_trade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # avg cash flow per trade
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # manually added, never auto-dropped
    leaderboard_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # overall leaderboard rank
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    snapshots: Mapped[list["TraderSnapshot"]] = relationship(
        "TraderSnapshot", back_populates="trader", cascade="all, delete-orphan"
    )
    signals: Mapped[list["Signal"]] = relationship(
        "Signal", back_populates="trader", cascade="all, delete-orphan"
    )


class TraderSnapshot(Base):
    """Historical daily snapshots of trader scores and stats."""

    __tablename__ = "trader_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trader_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("traders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_30d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consistency_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trade_count_30d: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    categories_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    trader: Mapped["Trader"] = relationship("Trader", back_populates="snapshots")


class TraderPnlSnapshot(Base):
    """
    Weekly leaderboard PnL snapshots per trader.
    This is the only way to build a real equity curve — collect one snapshot
    per discovery run and compute curve consistency over time.
    """

    __tablename__ = "trader_pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trader_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leaderboard_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    leaderboard_volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leaderboard_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    open_position_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Signal(Base):
    """Detected trade signals from watched traders."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trader_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("traders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    market_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    raw_trade_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    action_taken: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # copied / skipped / manual / paper
    skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strategies_triggered: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # slugs of strategies that said yes
    strategy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )

    trader: Mapped["Trader"] = relationship("Trader", back_populates="signals")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="signal")
    strategy: Mapped[Optional["Strategy"]] = relationship("Strategy")


class Position(Base):
    """Open and closed trading positions."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_name: Mapped[str] = mapped_column(String(512), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    shares: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)  # open / closed
    is_shadow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    signal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    entry_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)      # e.g. "Yes", "No", outcome name
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # market close date
    trader_address: Mapped[Optional[str]] = mapped_column(String(42), nullable=True)  # which trader triggered this

    strategy: Mapped[Optional["Strategy"]] = relationship("Strategy")
    signal: Mapped[Optional["Signal"]] = relationship("Signal", back_populates="positions")
    executions: Mapped[list["Execution"]] = relationship(
        "Execution", back_populates="position", cascade="all, delete-orphan"
    )


class Execution(Base):
    """Individual order executions tied to positions."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    position: Mapped["Position"] = relationship("Position", back_populates="executions")


class Strategy(Base):
    """Available trading strategies."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    params: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    results: Mapped[list["StrategyResult"]] = relationship(
        "StrategyResult", back_populates="strategy", cascade="all, delete-orphan"
    )


class StrategyResult(Base):
    """Daily performance results per strategy."""

    __tablename__ = "strategy_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_deployed: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="results")


class Setting(Base):
    """Key-value runtime settings (editable via Telegram commands)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Report(Base):
    """Stored periodic reports."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    report_text: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MarketCache(Base):
    """Cached market data from Gamma API."""

    __tablename__ = "markets_cache"

    condition_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventLog(Base):
    """Audit log for all significant events."""

    __tablename__ = "events_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    data_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
