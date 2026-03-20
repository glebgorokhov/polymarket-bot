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


async def get_all_active_strategies() -> list[tuple]:
    """Return all active strategies as (orm_model, strategy_instance) pairs."""
    from db.repos.strategies import StrategyRepo
    from db.session import get_session
    async with get_session() as session:
        repo = StrategyRepo(session)
        active = await repo.get_all_active()
    result = []
    for strat_orm in active:
        try:
            instance = get_strategy(strat_orm.slug, strat_orm.params or {})
            result.append((strat_orm, instance))
        except ValueError:
            pass
    return result
