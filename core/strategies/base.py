"""
Abstract base class for all trading strategies.
All strategies must implement should_copy and should_exit.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseStrategy(ABC):
    """
    Abstract base for copytrading strategies.

    Each strategy decides whether to copy a detected signal and
    when to exit an open position.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """
        Initialize with optional configuration parameters.

        Args:
            params: Strategy-specific parameters loaded from the DB.
        """
        self.params: dict[str, Any] = params or {}

    @property
    def name(self) -> str:
        """Human-readable strategy name."""
        return self.__class__.__name__

    @abstractmethod
    async def should_copy(
        self,
        signal: Any,
        all_recent_signals: list[Any],
        open_positions: list[Any],
    ) -> tuple[bool, float]:
        """
        Decide whether to copy a detected signal.

        Args:
            signal: The Signal ORM object detected from a tracked trader.
            all_recent_signals: List of all Signal objects seen in recent window.
            open_positions: List of currently open Position ORM objects.

        Returns:
            Tuple of (should_copy: bool, conviction_multiplier: float).
            conviction_multiplier scales position size (1.0 = normal, 2.0 = double).
        """

    @abstractmethod
    async def should_exit(
        self,
        position: Any,
        current_price: float,
        original_trader_exited: bool,
    ) -> tuple[bool, str]:
        """
        Decide whether to close an open position.

        Args:
            position: The Position ORM object.
            current_price: Latest market price for the token.
            original_trader_exited: True if the tracked trader closed their position.

        Returns:
            Tuple of (should_exit: bool, reason: str).
        """
