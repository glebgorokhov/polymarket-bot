"""
Strategy B: Consensus Gate.
Only enters a position when 2+ tracked traders agree on the same market/side
within a configurable time window.
Higher consensus → higher conviction multiplier.
"""

import logging
from typing import Any

from core.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class ConsensusStrategy(BaseStrategy):
    """
    Consensus Gate Strategy.

    Requires a minimum number of independent tracked traders to signal
    the same market and side within the configured window before copying.

    Params:
        min_traders (int): Minimum traders required. Default 2.
        window_minutes (int): Look-back window for matching signals. Default 10.
    """

    @property
    def name(self) -> str:
        return "Consensus Gate"

    def _min_traders(self) -> int:
        return int(self.params.get("min_traders", 2))

    def _window_minutes(self) -> int:
        return int(self.params.get("window_minutes", 10))

    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Copy only if min_traders independent traders agree on market + side.

        Conviction multiplier:
          - 2 traders → 1.5x
          - 3+ traders → 2.0x

        Args:
            signal: The incoming Signal to evaluate.
            all_recent_signals: All recent signals within the window.
            open_positions: Currently open positions (unused here).

        Returns:
            (should_copy, conviction_multiplier)
        """
        min_traders = self._min_traders()

        # Count unique trader IDs for same market + side in the recent window
        # (signal itself counts as 1)
        matching_trader_ids: set[int] = {signal.trader_id}
        for s in all_recent_signals:
            if (
                s.market_condition_id == signal.market_condition_id
                and s.side == signal.side
                and s.trader_id != signal.trader_id
            ):
                matching_trader_ids.add(s.trader_id)

        count = len(matching_trader_ids)
        logger.debug(
            "Consensus: %d unique traders on %s %s (need %d)",
            count,
            signal.market_condition_id,
            signal.side,
            min_traders,
        )

        if count < min_traders:
            return False, 1.0

        # Scale conviction by number of agreeing traders
        if count >= 3:
            conviction = float(self.params.get("conviction_multiplier_3", 2.0))
        else:
            conviction = float(self.params.get("conviction_multiplier_2", 1.5))

        return True, conviction

    async def should_exit(
        self,
        position: Any,
        current_price: float,
        original_trader_exited: bool,
    ) -> tuple[bool, str]:
        """
        Exit when the original trader exits.

        Args:
            position: Open Position object.
            current_price: Latest token price.
            original_trader_exited: Whether the tracked trader closed this position.

        Returns:
            (should_exit, reason)
        """
        if original_trader_exited:
            return True, "trader_exited"
        return False, ""
