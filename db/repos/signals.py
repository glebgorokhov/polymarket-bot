"""
Repository for Signal DB operations.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Signal


class SignalRepo:
    """Data access layer for the signals table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        trader_id: int,
        market_condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        raw_trade_id: Optional[str] = None,
        action_taken: Optional[str] = None,
        skip_reason: Optional[str] = None,
        strategy_id: Optional[int] = None,
    ) -> Signal:
        """Insert a new detected signal."""
        signal = Signal(
            trader_id=trader_id,
            market_condition_id=market_condition_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            raw_trade_id=raw_trade_id,
            action_taken=action_taken,
            skip_reason=skip_reason,
            strategy_id=strategy_id,
        )
        self._session.add(signal)
        await self._session.flush()
        return signal

    async def get_by_id(self, signal_id: int) -> Optional[Signal]:
        """Fetch a signal by primary key."""
        return await self._session.get(Signal, signal_id)

    async def update_strategies_triggered(
        self,
        signal_id: int,
        slugs: list[str],
        market_name: Optional[str] = None,
    ) -> None:
        """Store the list of strategy slugs that triggered on this signal."""
        signal = await self._session.get(Signal, signal_id)
        if signal:
            signal.strategies_triggered = slugs
            if market_name:
                signal.market_name = market_name

    async def update_action(
        self,
        signal_id: int,
        action_taken: str,
        skip_reason: Optional[str] = None,
    ) -> None:
        """Update the action taken for a signal (e.g., after decision)."""
        signal = await self._session.get(Signal, signal_id)
        if signal:
            signal.action_taken = action_taken
            signal.skip_reason = skip_reason

    async def get_latest(self, limit: int = 20) -> Sequence[Signal]:
        """Return the most recent N signals across all traders."""
        from db.models import Trader
        result = await self._session.execute(
            select(Signal)
            .join(Trader, Signal.trader_id == Trader.id)
            .order_by(Signal.detected_at.desc())
            .limit(limit)
            .options(__import__('sqlalchemy.orm', fromlist=['joinedload']).joinedload(Signal.trader))
        )
        return result.scalars().all()

    async def get_recent(self, hours: int = 1) -> Sequence[Signal]:
        """Return signals detected in the past N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await self._session.execute(
            select(Signal)
            .where(Signal.detected_at >= cutoff)
            .order_by(Signal.detected_at.desc())
        )
        return result.scalars().all()

    async def get_by_trader(self, trader_id: int, limit: int = 5) -> Sequence[Signal]:
        """Return the latest signals for a specific trader."""
        result = await self._session.execute(
            select(Signal)
            .where(Signal.trader_id == trader_id)
            .order_by(Signal.detected_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_by_address(self, address: str, limit: int = 5) -> Sequence[Signal]:
        """Return the latest signals from a trader identified by address (joins traders)."""
        from db.models import Trader

        result = await self._session.execute(
            select(Signal)
            .join(Trader, Signal.trader_id == Trader.id)
            .where(Trader.address == address)
            .order_by(Signal.detected_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def count_in_period(self, period_start: datetime, period_end: datetime) -> dict:
        """Return counts of signals by action_taken for a period."""
        result = await self._session.execute(
            select(Signal).where(
                Signal.detected_at >= period_start,
                Signal.detected_at <= period_end,
            )
        )
        signals = result.scalars().all()
        counts: dict = {"detected": len(signals), "copied": 0, "skipped": 0, "manual": 0}
        for s in signals:
            if s.action_taken == "copied":
                counts["copied"] += 1
            elif s.action_taken == "skipped":
                counts["skipped"] += 1
            elif s.action_taken == "manual":
                counts["manual"] += 1
        return counts

    async def get_recent_for_market(
        self,
        market_condition_id: str,
        side: str,
        within_minutes: int = 10,
    ) -> Sequence[Signal]:
        """Return recent signals for a specific market/side combo (for consensus strategy)."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        result = await self._session.execute(
            select(Signal).where(
                Signal.market_condition_id == market_condition_id,
                Signal.side == side,
                Signal.detected_at >= cutoff,
            )
        )
        return result.scalars().all()
