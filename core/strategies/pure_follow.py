"""
Strategy A: Pure Follow.
Copy every trade proportionally. Exit when the trader exits.
Simplest possible mirroring strategy.
"""

import logging
from typing import Any

from core.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class PureFollowStrategy(BaseStrategy):
    """
    Pure Follow Strategy.

    Copies every signal from a tracked trader with no filtering.
    Exits a position exactly when the original trader exits.
    Conviction multiplier is always 1.0 (no amplification).
    """

    @property
    def name(self) -> str:
        return "Pure Follow"

    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Always copy the signal.

        Args:
            signal: Detected Signal object.
            all_recent_signals: Unused for this strategy.
            open_positions: Unused for this strategy.

        Returns:
            (True, 1.0) — always copy with standard size.
        """
        logger.debug(
            "PureFollow: copying signal for market %s side %s",
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
        Exit only when the original trader exits.

        Args:
            position: Open Position object.
            current_price: Current token price.
            original_trader_exited: Whether the tracked trader closed this position.

        Returns:
            (True, "trader_exited") if trader exited, else (False, "").
        """
        if original_trader_exited:
            return True, "trader_exited"
        return False, ""
