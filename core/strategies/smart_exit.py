"""
Strategy E: Smart Exit.
Copies all signals (like Pure Follow) but uses intelligent exit rules:
trailing stop from peak, take-profit at high probability, and time-based exit.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_DEFAULT_TRAIL_STOP_PCT = 25.0
_DEFAULT_TAKE_PROFIT_PROB = 0.85
_RESOLVING_THRESHOLD_HOURS = 24


class SmartExitStrategy(BaseStrategy):
    """
    Smart Exit Strategy.

    Enters positions the same as Pure Follow (always), but applies
    sophisticated exit rules rather than blindly mirroring the trader's exit.

    Exit triggers (first one hit wins):
    1. Trailing stop: price drops 25% from peak value
    2. Take profit: probability > 0.85 (token price > 0.85)
    3. Time: market resolves within 24h AND position is profitable

    Params:
        trail_stop_pct (float): Percent drop from peak to trigger stop. Default 25.
        take_profit_prob (float): Price threshold for early take-profit. Default 0.85.
    """

    @property
    def name(self) -> str:
        return "Smart Exit"

    def _trail_stop_pct(self) -> float:
        return float(self.params.get("trail_stop_pct", _DEFAULT_TRAIL_STOP_PCT))

    def _take_profit_prob(self) -> float:
        return float(self.params.get("take_profit_prob", _DEFAULT_TAKE_PROFIT_PROB))

    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Always copy the signal (entry logic same as Pure Follow).

        Args:
            signal: The incoming Signal object.
            all_recent_signals: Unused here.
            open_positions: Unused here.

        Returns:
            (True, 1.0) — always enter.
        """
        logger.debug(
            "SmartExit: copying signal for market %s side %s",
            signal.market_condition_id,
            signal.side,
        )
        return True, 1.0

    async def should_exit(
        self,
        position: Any,
        current_price: float,
        original_trader_exited: bool,
    ) -> tuple[bool, str]:
        """
        Apply smart exit rules to an open position.

        Args:
            position: Open Position ORM object. Expected attributes:
                      peak_price (optional float, set externally),
                      entry_price (float),
                      market_end_date (optional datetime, set externally),
                      pnl (optional float).
            current_price: Latest token price.
            original_trader_exited: Whether the tracked trader closed.

        Returns:
            (should_exit, reason)
        """
        trail_stop_pct = self._trail_stop_pct()
        take_profit_prob = self._take_profit_prob()

        # 1. Check trailing stop from peak
        peak_price = getattr(position, "peak_price", None) or position.entry_price
        if peak_price and peak_price > 0:
            drop_pct = ((peak_price - current_price) / peak_price) * 100.0
            if drop_pct >= trail_stop_pct:
                logger.debug(
                    "SmartExit: trailing stop triggered (drop %.1f%% >= %.1f%%)",
                    drop_pct,
                    trail_stop_pct,
                )
                return True, f"trail_stop_{drop_pct:.1f}pct"

        # 2. Check take-profit threshold (probability near 1.0)
        if current_price >= take_profit_prob:
            logger.debug(
                "SmartExit: take-profit triggered (price %.3f >= %.3f)",
                current_price,
                take_profit_prob,
            )
            return True, "take_profit"

        # 3. Check time-based exit: market resolving within 24h AND profitable
        market_end_date = getattr(position, "market_end_date", None)
        if market_end_date:
            now = datetime.now(timezone.utc)
            hours_remaining = (market_end_date - now).total_seconds() / 3600
            if hours_remaining <= _RESOLVING_THRESHOLD_HOURS:
                entry_cost = position.entry_cost or position.size_usd
                current_value = current_price * (position.shares or 0)
                if current_value > entry_cost:
                    logger.debug(
                        "SmartExit: time-based exit triggered (%.1fh remaining, profitable)",
                        hours_remaining,
                    )
                    return True, "resolving_profitable"

        return False, ""
