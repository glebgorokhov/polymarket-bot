"""
Repository for Strategy and StrategyResult DB operations.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Strategy, StrategyResult


class StrategyRepo:
    """Data access layer for the strategies table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> Sequence[Strategy]:
        """Return all strategies ordered by name."""
        result = await self._session.execute(select(Strategy).order_by(Strategy.name))
        return result.scalars().all()

    async def get_active(self) -> Optional[Strategy]:
        """Return the currently active strategy (is_active=True)."""
        result = await self._session.execute(
            select(Strategy).where(Strategy.is_active.is_(True)).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Strategy]:
        """Fetch a strategy by its slug identifier."""
        result = await self._session.execute(
            select(Strategy).where(Strategy.slug == slug)
        )
        return result.scalar_one_or_none()

    async def set_active(self, slug: str) -> bool:
        """Deactivate all strategies then activate the specified one. Returns True if found."""
        await self._session.execute(update(Strategy).values(is_active=False))
        strategy = await self.get_by_slug(slug)
        if strategy is None:
            return False
        strategy.is_active = True
        return True

    async def count(self) -> int:
        """Return the count of all strategies."""
        result = await self._session.execute(select(Strategy))
        return len(result.scalars().all())

    async def create(
        self,
        name: str,
        slug: str,
        description: str,
        params: Optional[dict] = None,
        is_active: bool = False,
    ) -> Strategy:
        """Insert a new strategy."""
        strategy = Strategy(
            name=name,
            slug=slug,
            description=description,
            params=params or {},
            is_active=is_active,
        )
        self._session.add(strategy)
        await self._session.flush()
        return strategy

    async def get_7d_pnl(self, strategy_id: int) -> float:
        """Return summed PnL from strategy_results over the last 7 days."""
        cutoff = date.today() - timedelta(days=7)
        result = await self._session.execute(
            select(StrategyResult).where(
                StrategyResult.strategy_id == strategy_id,
                StrategyResult.date >= cutoff,
            )
        )
        rows = result.scalars().all()
        return sum(r.pnl for r in rows)


class StrategyResultRepo:
    """Data access layer for the strategy_results table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        strategy_id: int,
        pnl: float,
        trades_count: int = 0,
        win_count: int = 0,
        total_deployed: float = 0.0,
        result_date: Optional[date] = None,
    ) -> StrategyResult:
        """Insert a daily result record for a strategy."""
        record_date = result_date or date.today()
        sr = StrategyResult(
            strategy_id=strategy_id,
            date=record_date,
            pnl=pnl,
            trades_count=trades_count,
            win_count=win_count,
            total_deployed=total_deployed,
        )
        self._session.add(sr)
        await self._session.flush()
        return sr

    async def get_for_strategy(
        self, strategy_id: int, days: int = 7
    ) -> Sequence[StrategyResult]:
        """Return recent result rows for a strategy."""
        cutoff = date.today() - timedelta(days=days)
        result = await self._session.execute(
            select(StrategyResult).where(
                StrategyResult.strategy_id == strategy_id,
                StrategyResult.date >= cutoff,
            ).order_by(StrategyResult.date.desc())
        )
        return result.scalars().all()
