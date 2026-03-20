"""
Strategy D: Category Expert.
Only copies signals from a trader when trading in a market category
where that trader has demonstrated a high historical win rate.
"""

import logging
from typing import Any

from core.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_DEFAULT_MIN_STRENGTH = 0.6


class CategoryExpertStrategy(BaseStrategy):
    """
    Category Expert Strategy.

    Filters signals by checking whether the originating trader has a
    category_strength score above min_strength for the target market's category.

    Params:
        min_strength (float): Minimum win-rate strength to copy. Default 0.6.
    """

    @property
    def name(self) -> str:
        return "Category Expert"

    def _min_strength(self) -> float:
        return float(self.params.get("min_strength", _DEFAULT_MIN_STRENGTH))

    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Copy only if the trader is an expert in the signal's market category.

        Expects signal.market_category to be set during signal validation,
        and signal.trader.category_strengths to be a dict of {category: strength}.

        Args:
            signal: The incoming Signal with market_category attribute.
            all_recent_signals: Unused here.
            open_positions: Unused here.

        Returns:
            (should_copy, conviction_multiplier)
        """
        min_strength = self._min_strength()

        # Access the category from the signal's enriched attributes
        category = getattr(signal, "market_category", None)
        if not category:
            logger.debug("CategoryExpert: no category info on signal, skipping")
            return False, 1.0

        # Access the trader's category strengths
        trader = getattr(signal, "trader", None)
        if trader is None:
            logger.debug("CategoryExpert: no trader info on signal, skipping")
            return False, 1.0

        strengths: dict = trader.category_strengths or {}
        strength = strengths.get(category, 0.0)

        logger.debug(
            "CategoryExpert: trader strength in %s = %.2f (min %.2f)",
            category,
            strength,
            min_strength,
        )

        if strength >= min_strength:
            # Conviction scales with expertise level
            conviction = 1.0 + (strength - min_strength)  # 1.0–1.4 range for 0.6–1.0 strength
            return True, min(conviction, 2.0)
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
