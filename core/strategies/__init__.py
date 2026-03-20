"""Trading strategies package."""

from core.strategies.base import BaseStrategy
from core.strategies.pure_follow import PureFollowStrategy
from core.strategies.consensus import ConsensusStrategy
from core.strategies.whale import WhaleStrategy
from core.strategies.category_expert import CategoryExpertStrategy
from core.strategies.smart_exit import SmartExitStrategy

__all__ = [
    "BaseStrategy",
    "PureFollowStrategy",
    "ConsensusStrategy",
    "WhaleStrategy",
    "CategoryExpertStrategy",
    "SmartExitStrategy",
]

STRATEGY_MAP: dict[str, type[BaseStrategy]] = {
    "pure_follow": PureFollowStrategy,
    "consensus": ConsensusStrategy,
    "whale": WhaleStrategy,
    "category_expert": CategoryExpertStrategy,
    "smart_exit": SmartExitStrategy,
}


def get_strategy(slug: str, params: dict | None = None) -> BaseStrategy:
    """
    Instantiate a strategy by its slug identifier.

    Args:
        slug: Strategy identifier string.
        params: Optional configuration params from DB.

    Returns:
        Instantiated BaseStrategy subclass.

    Raises:
        ValueError: If slug is unknown.
    """
    cls = STRATEGY_MAP.get(slug)
    if cls is None:
        raise ValueError(f"Unknown strategy slug: {slug!r}")
    return cls(params=params or {})
