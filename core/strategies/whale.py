"""
Strategy C: Whale Entry.
Only copies signals where the trader's bet size exceeds a multiple
of their historical average trade size (indicating high conviction).
"""

import logging
from typing import Any

from core.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_DEFAULT_SIZE_MULTIPLIER = 2.0
_FALLBACK_AVG_TRADE_SIZE = 50.0  # USD fallback when no history available


class WhaleStrategy(BaseStrategy):
    """
    Whale Entry Strategy.

    Filters signals by requiring the trader's current trade size
    to exceed size_multiplier × their average historical trade size.

    Params:
        size_multiplier (float): Required multiple of average. Default 2.0.
    """

    @property
    def name(self) -> str:
        return "Whale Entry"

    def _size_multiplier(self) -> float:
        return float(self.params.get("size_multiplier", _DEFAULT_SIZE_MULTIPLIER))

    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Copy only if signal size exceeds size_multiplier × trader's average size.

        The average is estimated from recent_signals for the same trader.
        If no history is available, falls back to _FALLBACK_AVG_TRADE_SIZE.

        Args:
            signal: The incoming Signal to evaluate.
            all_recent_signals: Recent signals for computing averages.
            open_positions: Unused here.

        Returns:
            (should_copy, conviction_multiplier)
        """
        multiplier = self._size_multiplier()

        # Compute trader's average trade size from recent signals
        trader_signals = [
            s for s in all_recent_signals
            if s.trader_id == signal.trader_id and s.size_usd > 0
        ]

        if trader_signals:
            avg_size = sum(s.size_usd for s in trader_signals) / len(trader_signals)
        else:
            avg_size = _FALLBACK_AVG_TRADE_SIZE

        threshold = avg_size * multiplier

        logger.debug(
            "Whale: signal size=%.2f avg=%.2f threshold=%.2f (x%.1f)",
            signal.size_usd,
            avg_size,
            threshold,
            multiplier,
        )

        if signal.size_usd >= threshold:
            return True, 1.5  # Whale trades get a slight extra conviction
        return False, 1.0

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
