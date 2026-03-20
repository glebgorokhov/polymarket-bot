"""
Risk management module.
Handles position sizing and pre-trade risk checks.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def calculate_trade_size(
    available_balance: float,
    per_trade_pct: float,
    max_trade_usd: float,
    conviction_multiplier: float = 1.0,
) -> float:
    """
    Calculate the USD size for a new trade.

    Formula: balance * (pct / 100) * multiplier, capped at max_trade_usd.

    Args:
        available_balance: Total available USDC balance.
        per_trade_pct: Percentage of balance to use per trade (e.g. 5.0 for 5%).
        max_trade_usd: Hard cap per trade in USD.
        conviction_multiplier: Scale factor from strategy (default 1.0).

    Returns:
        Proposed trade size in USD (>= 0).
    """
    base_size = available_balance * (per_trade_pct / 100.0) * conviction_multiplier
    size = min(base_size, max_trade_usd)
    logger.debug(
        "Trade size: balance=%.2f pct=%.1f%% mult=%.2f → base=%.2f cap=%.2f → %.2f",
        available_balance,
        per_trade_pct,
        conviction_multiplier,
        base_size,
        max_trade_usd,
        size,
    )
    return max(0.0, size)


async def check_risk_limits(
    market_condition_id: str,
    proposed_size: float,
    open_positions: list[Any],
    total_balance: float,
    settings: dict[str, str],
) -> tuple[bool, str]:
    """
    Validate a proposed trade against risk limits.

    Checks performed:
    1. Total exposure cap: deployed_capital / total_balance <= max_total_exposure_pct
    2. Per-market cap: sum of open positions in this market <= per_market_cap_pct * balance
    3. Cool-down: no losses exceeding threshold in last 24 hours
    4. Daily drawdown: cumulative loss today <= max_drawdown_pct * balance

    Args:
        market_condition_id: The market being traded.
        proposed_size: USD size of the proposed trade.
        open_positions: List of all open Position ORM objects.
        total_balance: Total available balance (USDC).
        settings: Settings dict from DB (keys as strings, values as strings).

    Returns:
        (ok: bool, reason: str) — reason is empty string if ok.
    """
    def _get_float(key: str, default: float) -> float:
        try:
            return float(settings.get(key, default))
        except (ValueError, TypeError):
            return default

    max_total_exposure_pct = _get_float("max_total_exposure_pct", 60.0)
    per_market_cap_pct = _get_float("per_market_cap_pct", 20.0)
    max_drawdown_pct = _get_float("max_drawdown_pct", 15.0)

    if total_balance <= 0:
        return False, "zero_balance"

    # 1. Total exposure check
    total_deployed = sum(p.size_usd for p in open_positions)
    new_deployed = total_deployed + proposed_size
    exposure_pct = (new_deployed / total_balance) * 100.0
    if exposure_pct > max_total_exposure_pct:
        reason = f"total_exposure_{exposure_pct:.1f}%>limit_{max_total_exposure_pct}%"
        logger.warning("Risk: %s", reason)
        return False, reason

    # 2. Per-market cap check
    market_deployed = sum(
        p.size_usd for p in open_positions
        if p.market_condition_id == market_condition_id
    )
    market_new = market_deployed + proposed_size
    market_pct = (market_new / total_balance) * 100.0
    if market_pct > per_market_cap_pct:
        reason = f"per_market_{market_pct:.1f}%>limit_{per_market_cap_pct}%"
        logger.warning("Risk: %s", reason)
        return False, reason

    # 3 & 4. Drawdown checks require closed position data — evaluated by caller
    # (the executor has access to the position repo for loss history)

    logger.debug(
        "Risk OK: exposure=%.1f%% market_pct=%.1f%%",
        exposure_pct,
        market_pct,
    )
    return True, ""


async def check_drawdown_limit(
    closed_positions_today: list[Any],
    total_balance: float,
    settings: dict[str, str],
) -> tuple[bool, str]:
    """
    Check if daily drawdown limit has been breached.

    Args:
        closed_positions_today: Positions closed today (may include PnL).
        total_balance: Current total balance.
        settings: Settings dict.

    Returns:
        (ok: bool, reason: str)
    """
    def _get_float(key: str, default: float) -> float:
        try:
            return float(settings.get(key, default))
        except (ValueError, TypeError):
            return default

    max_drawdown_pct = _get_float("max_drawdown_pct", 15.0)
    if total_balance <= 0:
        return False, "zero_balance"

    total_loss_today = sum(
        p.pnl for p in closed_positions_today
        if p.pnl is not None and p.pnl < 0
    )
    drawdown_pct = abs(total_loss_today / total_balance) * 100.0
    if drawdown_pct >= max_drawdown_pct:
        reason = f"daily_drawdown_{drawdown_pct:.1f}%>={max_drawdown_pct}%"
        logger.warning("Risk: %s", reason)
        return False, reason

    return True, ""
