"""
Repository for Position and Execution DB operations.
"""

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Execution, Position


class PositionRepo:
    """Data access layer for the positions table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        market_condition_id: str,
        token_id: str,
        market_name: str,
        side: str,
        entry_price: float,
        size_usd: float,
        shares: Optional[float] = None,
        strategy_id: Optional[int] = None,
        signal_id: Optional[int] = None,
        entry_cost: Optional[float] = None,
        is_shadow: bool = False,
    ) -> Position:
        """Insert a new open position."""
        position = Position(
            market_condition_id=market_condition_id,
            token_id=token_id,
            market_name=market_name,
            side=side,
            entry_price=entry_price,
            current_price=entry_price,
            size_usd=size_usd,
            shares=shares,
            status="open",
            strategy_id=strategy_id,
            signal_id=signal_id,
            entry_cost=entry_cost,
            is_shadow=is_shadow,
        )
        self._session.add(position)
        await self._session.flush()
        return position

    async def get_by_id(self, position_id: int) -> Optional[Position]:
        """Fetch position by primary key."""
        return await self._session.get(Position, position_id)

    async def get_open(self, is_shadow: bool | None = None) -> Sequence[Position]:
        """Return open positions. Filter by is_shadow if provided."""
        query = select(Position).where(Position.status == "open")
        if is_shadow is not None:
            query = query.where(Position.is_shadow == is_shadow)
        result = await self._session.execute(query.order_by(Position.opened_at.desc()))
        return result.scalars().all()

    async def get_open_by_strategy(self, strategy_id: int, is_shadow: bool = False) -> Sequence[Position]:
        """Return open positions for a specific strategy."""
        result = await self._session.execute(
            select(Position).where(
                Position.status == "open",
                Position.strategy_id == strategy_id,
                Position.is_shadow == is_shadow,
            ).order_by(Position.opened_at.desc())
        )
        return result.scalars().all()

    async def get_open_for_market(self, market_condition_id: str) -> Sequence[Position]:
        """Return open positions for a specific market."""
        result = await self._session.execute(
            select(Position).where(
                Position.status == "open",
                Position.market_condition_id == market_condition_id,
            )
        )
        return result.scalars().all()

    async def get_closed(self, limit: int = 10, is_shadow: bool | None = None) -> Sequence[Position]:
        """Return recently closed positions. Filter by is_shadow if provided."""
        query = select(Position).where(Position.status == "closed")
        if is_shadow is not None:
            query = query.where(Position.is_shadow == is_shadow)
        result = await self._session.execute(
            query.order_by(Position.closed_at.desc()).limit(limit)
        )
        return result.scalars().all()

    async def close_position(
        self,
        position_id: int,
        exit_value: float,
        close_reason: str,
    ) -> Optional[Position]:
        """Mark a position as closed and compute PnL."""
        position = await self.get_by_id(position_id)
        if position is None:
            return None
        position.status = "closed"
        position.closed_at = datetime.now(timezone.utc)
        position.exit_value = exit_value
        position.close_reason = close_reason
        entry_cost = position.entry_cost or position.size_usd
        position.pnl = exit_value - entry_cost
        if entry_cost > 0:
            position.pnl_pct = (position.pnl / entry_cost) * 100.0
        await self._session.flush()
        return position

    async def update_current_price(self, position_id: int, price: float) -> None:
        """Update the current market price for a position."""
        position = await self.get_by_id(position_id)
        if position:
            position.current_price = price

    async def get_closed_in_period(
        self, period_start: datetime, period_end: datetime
    ) -> Sequence[Position]:
        """Return positions closed within a time window."""
        result = await self._session.execute(
            select(Position).where(
                Position.status == "closed",
                Position.closed_at >= period_start,
                Position.closed_at <= period_end,
            )
        )
        return result.scalars().all()

    async def get_total_pnl(self) -> float:
        """Sum PnL of all closed positions."""
        result = await self._session.execute(
            select(Position).where(Position.status == "closed")
        )
        positions = result.scalars().all()
        return sum(p.pnl or 0.0 for p in positions)

    async def get_deployed_value(self) -> float:
        """Sum of size_usd for all open positions (deployed capital)."""
        result = await self._session.execute(
            select(Position).where(Position.status == "open")
        )
        positions = result.scalars().all()
        return sum(p.size_usd for p in positions)


class ExecutionRepo:
    """Data access layer for the executions table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        position_id: int,
        side: str,
        price: float,
        size: float,
        order_id: Optional[str] = None,
        fee: Optional[float] = None,
    ) -> Execution:
        """Insert a new execution record."""
        execution = Execution(
            position_id=position_id,
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            fee=fee,
        )
        self._session.add(execution)
        await self._session.flush()
        return execution

    async def get_for_position(self, position_id: int) -> Sequence[Execution]:
        """Return all executions for a position."""
        result = await self._session.execute(
            select(Execution)
            .where(Execution.position_id == position_id)
            .order_by(Execution.executed_at)
        )
        return result.scalars().all()
