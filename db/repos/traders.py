"""
Repository for Trader and TraderSnapshot DB operations.
All methods accept an AsyncSession and return ORM objects.
"""

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Trader, TraderSnapshot


class TraderRepo:
    """Data access layer for the traders table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_address(self, address: str) -> Optional[Trader]:
        """Fetch a trader by their on-chain address."""
        result = await self._session.execute(
            select(Trader).where(Trader.address == address)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, trader_id: int) -> Optional[Trader]:
        """Fetch a trader by primary key."""
        return await self._session.get(Trader, trader_id)

    async def get_active(self) -> Sequence[Trader]:
        """Return all traders with status='active'."""
        result = await self._session.execute(
            select(Trader).where(Trader.status == "active").order_by(Trader.score.desc())
        )
        return result.scalars().all()

    async def get_all(self) -> Sequence[Trader]:
        """Return all tracked traders ordered by score descending."""
        result = await self._session.execute(
            select(Trader).order_by(Trader.score.desc())
        )
        return result.scalars().all()

    async def upsert(
        self,
        address: str,
        display_name: Optional[str] = None,
        score: float = 0.0,
        status: str = "watching",
        category_strengths: Optional[dict] = None,
        total_pnl: float = 0.0,
        monthly_pnl_history: Optional[list] = None,
        weekly_pnl_history: Optional[list] = None,
        trade_count: int = 0,
        win_rate: Optional[float] = None,
        avg_trades_per_week: Optional[float] = None,
        avg_profit_per_trade: Optional[float] = None,
        leaderboard_rank: Optional[int] = None,
        first_seen_at: Optional[datetime] = None,
        last_active_at: Optional[datetime] = None,
    ) -> Trader:
        """Insert or update a trader record by address."""
        trader = await self.get_by_address(address)
        if trader is None:
            trader = Trader(
                address=address,
                display_name=display_name,
                score=score,
                status=status,
                category_strengths=category_strengths,
                total_pnl=total_pnl,
                monthly_pnl_history=monthly_pnl_history,
                weekly_pnl_history=weekly_pnl_history,
                trade_count=trade_count,
                win_rate=win_rate,
                avg_trades_per_week=avg_trades_per_week,
                avg_profit_per_trade=avg_profit_per_trade,
                leaderboard_rank=leaderboard_rank,
                first_seen_at=first_seen_at,
                last_active_at=last_active_at,
            )
            self._session.add(trader)
        else:
            if display_name is not None:
                trader.display_name = display_name
            trader.score = score
            trader.status = status
            if category_strengths is not None:
                trader.category_strengths = category_strengths
            trader.total_pnl = total_pnl
            if monthly_pnl_history is not None:
                trader.monthly_pnl_history = monthly_pnl_history
            if weekly_pnl_history is not None:
                trader.weekly_pnl_history = weekly_pnl_history
            trader.trade_count = trade_count
            if win_rate is not None:
                trader.win_rate = win_rate
            if avg_trades_per_week is not None:
                trader.avg_trades_per_week = avg_trades_per_week
            if avg_profit_per_trade is not None:
                trader.avg_profit_per_trade = avg_profit_per_trade
            if leaderboard_rank is not None:
                trader.leaderboard_rank = leaderboard_rank
            # Never overwrite is_pinned — managed by /track and /untrack commands
            if first_seen_at is not None:
                trader.first_seen_at = first_seen_at
            if last_active_at is not None:
                trader.last_active_at = last_active_at
        await self._session.flush()
        return trader

    async def pin(self, address: str) -> Optional[Trader]:
        """Pin a trader so they are never auto-dropped by discovery."""
        trader = await self.get_by_address(address)
        if trader:
            trader.is_pinned = True
            trader.status = "active"
            await self._session.flush()
        return trader

    async def unpin(self, address: str) -> Optional[Trader]:
        """Unpin a trader (allow auto-management by discovery again)."""
        trader = await self.get_by_address(address)
        if trader:
            trader.is_pinned = False
            await self._session.flush()
        return trader

    async def update(self, address: str, **kwargs) -> Optional["Trader"]:
        """Update arbitrary fields on a trader by address."""
        trader = await self.get_by_address(address)
        if trader is None:
            return None
        for key, value in kwargs.items():
            if hasattr(trader, key):
                setattr(trader, key, value)
        await self._session.flush()
        return trader

    async def update_status(self, trader_id: int, status: str) -> None:
        """Update the status field for a trader."""
        await self._session.execute(
            update(Trader).where(Trader.id == trader_id).values(status=status)
        )

    async def update_score(self, trader_id: int, score: float) -> None:
        """Update the composite score for a trader."""
        await self._session.execute(
            update(Trader).where(Trader.id == trader_id).values(score=score)
        )

    async def count_active(self) -> int:
        """Return the count of active traders."""
        result = await self._session.execute(
            select(Trader).where(Trader.status == "active")
        )
        return len(result.scalars().all())

    async def count_all(self) -> int:
        """Return the total count of tracked traders."""
        result = await self._session.execute(select(Trader))
        return len(result.scalars().all())


class TraderPnlSnapshotRepo:
    """
    Stores weekly leaderboard PnL snapshots per trader.
    Used to build equity curves over time for curve consistency scoring.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        address: str,
        leaderboard_pnl: float,
        leaderboard_volume: Optional[float] = None,
        leaderboard_rank: Optional[int] = None,
        open_position_value: Optional[float] = None,
    ) -> None:
        from db.models import TraderPnlSnapshot
        from datetime import timezone
        snap = TraderPnlSnapshot(
            trader_address=address.lower(),
            captured_at=datetime.now(timezone.utc),
            leaderboard_pnl=leaderboard_pnl,
            leaderboard_volume=leaderboard_volume,
            leaderboard_rank=leaderboard_rank,
            open_position_value=open_position_value,
        )
        self._session.add(snap)

    async def get_history(self, address: str, limit: int = 52) -> list:
        """
        Return PnL snapshots for a trader, oldest first, up to `limit` weeks.
        """
        from db.models import TraderPnlSnapshot
        result = await self._session.execute(
            select(TraderPnlSnapshot)
            .where(TraderPnlSnapshot.trader_address == address.lower())
            .order_by(TraderPnlSnapshot.captured_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def compute_curve_consistency(self, address: str) -> Optional[float]:
        """
        Compute R² + drawdown score from stored PnL snapshots.
        Returns None if fewer than 3 snapshots (not enough data).
        """
        import math
        snaps = await self.get_history(address, limit=52)
        if len(snaps) < 3:
            return None

        values = [s.leaderboard_pnl for s in snaps]
        n = len(values)
        x = list(range(n))
        x_mean = sum(x) / n
        y_mean = sum(values) / n
        ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, values))
        ss_xx = sum((xi - x_mean) ** 2 for xi in x)
        ss_yy = sum((yi - y_mean) ** 2 for yi in values)
        r2 = (ss_xy ** 2) / (ss_xx * ss_yy) if (ss_xx * ss_yy) > 0 else 0

        # Max drawdown as % of final PnL
        peak = values[0]
        max_dd = 0
        for v in values:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd

        final_pnl = max(abs(values[-1]), 1)
        dd_score = max(0.0, 1.0 - (max_dd / final_pnl))

        # Combined: R² weighted more
        return round(r2 * 0.6 + dd_score * 0.4, 4)


class TraderSnapshotRepo:
    """Data access layer for the trader_snapshots table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        trader_id: int,
        date,
        score: float,
        pnl_30d: Optional[float] = None,
        sharpe_ratio: Optional[float] = None,
        consistency_score: Optional[float] = None,
        trade_count_30d: Optional[int] = None,
        categories_json: Optional[dict] = None,
    ) -> TraderSnapshot:
        """Insert a new daily snapshot for a trader."""
        snapshot = TraderSnapshot(
            trader_id=trader_id,
            date=date,
            score=score,
            pnl_30d=pnl_30d,
            sharpe_ratio=sharpe_ratio,
            consistency_score=consistency_score,
            trade_count_30d=trade_count_30d,
            categories_json=categories_json,
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def get_latest(self, trader_id: int) -> Optional[TraderSnapshot]:
        """Return the most recent snapshot for a trader."""
        result = await self._session.execute(
            select(TraderSnapshot)
            .where(TraderSnapshot.trader_id == trader_id)
            .order_by(TraderSnapshot.date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
