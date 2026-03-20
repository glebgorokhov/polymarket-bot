"""
Trader discovery and scoring engine.
Pulls Polymarket leaderboard data, scores traders using a composite formula,
and manages the set of tracked wallets in the DB.
"""

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.data_api import DataApiClient
from api.gamma import GammaApiClient
from db.models import Trader
from db.repos.traders import TraderRepo, TraderSnapshotRepo
from db.session import get_session

logger = logging.getLogger(__name__)

_TOP_N = 10  # Number of traders to track
_SCORE_THRESHOLD = 0.4  # Minimum composite score to stay active
_LEADERBOARD_LIMIT = 100  # How many leaderboard entries to fetch


def _compute_consistency_score(monthly_pnl_history: list[dict]) -> float:
    """
    Compute consistency score from monthly PnL history.

    consistency = months_profitable / 6 (last 6 months).

    Args:
        monthly_pnl_history: List of {month: str, pnl: float} dicts.

    Returns:
        Consistency score in [0, 1].
    """
    if not monthly_pnl_history:
        return 0.0
    recent = monthly_pnl_history[-6:]
    profitable = sum(1 for m in recent if m.get("pnl", 0) > 0)
    return profitable / 6.0


def _compute_sharpe_normalized(monthly_pnl_history: list[dict]) -> float:
    """
    Compute normalized Sharpe-like ratio from monthly returns.

    sharpe = mean / std (clamped 0-3, then /3 to normalize).

    Args:
        monthly_pnl_history: List of {month: str, pnl: float} dicts.

    Returns:
        Normalized Sharpe ratio in [0, 1].
    """
    if len(monthly_pnl_history) < 2:
        return 0.0
    returns = [m.get("pnl", 0.0) for m in monthly_pnl_history]
    mean_ret = statistics.mean(returns)
    std_ret = statistics.stdev(returns)
    if std_ret == 0:
        return 1.0 if mean_ret > 0 else 0.0
    raw_sharpe = mean_ret / std_ret
    clamped = max(0.0, min(3.0, raw_sharpe))
    return clamped / 3.0


def _compute_diversity_score(category_strengths: dict) -> float:
    """
    Compute diversity score from category strengths.

    diversity = unique_categories_with_activity / total_known_categories.

    Args:
        category_strengths: Dict of {category: strength}.

    Returns:
        Diversity score in [0, 1].
    """
    total_categories = 8  # Approximate number of Polymarket categories
    if not category_strengths:
        return 0.0
    active = sum(1 for v in category_strengths.values() if v > 0)
    return min(active / total_categories, 1.0)


def _compute_frequency_score(trades_per_month: float) -> float:
    """
    Compute frequency score from average monthly trade count.

    frequency = min(trades_per_month / 10, 1.0)

    Args:
        trades_per_month: Average number of trades per month.

    Returns:
        Frequency score in [0, 1].
    """
    return min(trades_per_month / 10.0, 1.0)


def _compute_recency_score(last_active_at: Optional[datetime]) -> float:
    """
    Compute recency score based on last activity timestamp.

    1.0 if active within 14d, 0.5 if within 30d, else 0.

    Args:
        last_active_at: Datetime of last known trade.

    Returns:
        Recency score: 0.0, 0.5, or 1.0.
    """
    if last_active_at is None:
        return 0.0
    now = datetime.now(timezone.utc)
    # Ensure timezone-aware comparison
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)
    days_ago = (now - last_active_at).days
    if days_ago <= 14:
        return 1.0
    if days_ago <= 30:
        return 0.5
    return 0.0


def compute_composite_score(
    monthly_pnl_history: list[dict],
    category_strengths: dict,
    trades_per_month: float,
    last_active_at: Optional[datetime],
) -> float:
    """
    Compute the composite trader quality score.

    Formula:
        composite = (consistency * 0.35) + (sharpe_norm * 0.25)
                  + (diversity * 0.15) + (frequency * 0.15)
                  + (recency * 0.10)

    Args:
        monthly_pnl_history: Monthly PnL records.
        category_strengths: Per-category win rates.
        trades_per_month: Average monthly trade count.
        last_active_at: Last trade timestamp.

    Returns:
        Composite score in [0, 1].
    """
    consistency = _compute_consistency_score(monthly_pnl_history)
    sharpe_norm = _compute_sharpe_normalized(monthly_pnl_history)
    diversity = _compute_diversity_score(category_strengths)
    frequency = _compute_frequency_score(trades_per_month)
    recency = _compute_recency_score(last_active_at)

    score = (
        consistency * 0.35
        + sharpe_norm * 0.25
        + diversity * 0.15
        + frequency * 0.15
        + recency * 0.10
    )
    logger.debug(
        "Score: consistency=%.2f sharpe=%.2f diversity=%.2f freq=%.2f recency=%.2f → %.3f",
        consistency,
        sharpe_norm,
        diversity,
        frequency,
        recency,
        score,
    )
    return round(score, 4)


async def compute_category_strengths(address: str) -> dict[str, float]:
    """
    Compute win rates per category for a trader from their trade history.

    Args:
        address: Trader's on-chain address.

    Returns:
        Dict of {category: win_rate} where win_rate is in [0, 1].
    """
    async with DataApiClient() as data_client:
        trades = await data_client.get_trades(user=address, limit=200)

    # Group trades by market and track outcomes
    # Trades with positive realized PnL are wins
    category_stats: dict[str, dict] = {}
    for trade in trades:
        category = trade.get("market", {}).get("category", "UNKNOWN") if isinstance(trade.get("market"), dict) else "UNKNOWN"
        if not category:
            category = "UNKNOWN"
        if category not in category_stats:
            category_stats[category] = {"wins": 0, "total": 0}
        pnl = float(trade.get("profit", trade.get("pnl", 0)) or 0)
        category_stats[category]["total"] += 1
        if pnl > 0:
            category_stats[category]["wins"] += 1

    strengths: dict[str, float] = {}
    for cat, stats in category_stats.items():
        if stats["total"] > 0:
            strengths[cat] = round(stats["wins"] / stats["total"], 3)

    return strengths


async def _build_monthly_pnl_history(trades: list[dict]) -> list[dict]:
    """
    Aggregate trades into monthly PnL records.

    Args:
        trades: Raw trade dicts from Data API.

    Returns:
        List of {month: "YYYY-MM", pnl: float} sorted ascending.
    """
    monthly: dict[str, float] = {}
    for trade in trades:
        ts_str = trade.get("timestamp", trade.get("createdAt", ""))
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            key = ts.strftime("%Y-%m")
            pnl = float(trade.get("profit", trade.get("pnl", 0)) or 0)
            monthly[key] = monthly.get(key, 0.0) + pnl
        except (ValueError, TypeError):
            continue

    return [{"month": k, "pnl": v} for k, v in sorted(monthly.items())]


async def discover_top_traders() -> None:
    """
    Pull the Polymarket leaderboard and store the top N traders in the DB.

    Fetches the ALL-time leaderboard, computes composite scores for each
    trader, selects the top _TOP_N by score, and upserts them with
    status='active'. Existing tracked traders not in the new top N
    are downgraded to status='watching'.
    """
    logger.info("Starting trader discovery (top %d)", _TOP_N)

    async with DataApiClient() as data_client:
        leaderboard = await data_client.get_leaderboard(
            category="ALL",
            time_period="ALL",
            order_by="profit",
            limit=_LEADERBOARD_LIMIT,
        )

    if not leaderboard:
        logger.warning("Leaderboard returned no results")
        return

    scored: list[tuple[float, dict]] = []
    for entry in leaderboard:
        address = entry.get("proxyWallet", entry.get("address", ""))
        if not address:
            continue
        try:
            async with DataApiClient() as data_client:
                trades = await data_client.get_trades(user=address, limit=200)
            monthly_history = await _build_monthly_pnl_history(trades)
            category_strengths = await compute_category_strengths(address)
            trades_per_month = len(trades) / max(len(monthly_history), 1)

            last_trade_ts: Optional[datetime] = None
            if trades:
                ts_str = trades[0].get("timestamp", trades[0].get("createdAt", ""))
                if ts_str:
                    last_trade_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

            score = compute_composite_score(
                monthly_pnl_history=monthly_history,
                category_strengths=category_strengths,
                trades_per_month=trades_per_month,
                last_active_at=last_trade_ts,
            )
            scored.append((
                score,
                {
                    "address": address,
                    "display_name": entry.get("name", entry.get("username", "")),
                    "score": score,
                    "category_strengths": category_strengths,
                    "total_pnl": float(entry.get("profit", entry.get("pnl", 0)) or 0),
                    "monthly_pnl_history": monthly_history,
                    "trade_count": len(trades),
                    "last_active_at": last_trade_ts,
                },
            ))
        except Exception as exc:
            logger.warning("Failed to score trader %s: %s", address, exc)
            continue

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:_TOP_N]
    top_addresses = {d["address"] for _, d in top}

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        snapshot_repo = TraderSnapshotRepo(session)

        # Downgrade existing active traders not in new top N
        existing = await trader_repo.get_active()
        for t in existing:
            if t.address not in top_addresses:
                logger.info("Downgrading trader %s to watching", t.address)
                await trader_repo.update_status(t.id, "watching")

        # Upsert new top traders
        for score, data in top:
            trader = await trader_repo.upsert(
                address=data["address"],
                display_name=data.get("display_name"),
                score=score,
                status="active",
                category_strengths=data.get("category_strengths"),
                total_pnl=data.get("total_pnl", 0.0),
                monthly_pnl_history=data.get("monthly_pnl_history"),
                trade_count=data.get("trade_count", 0),
                last_active_at=data.get("last_active_at"),
            )
            await snapshot_repo.create(
                trader_id=trader.id,
                date=datetime.now(timezone.utc).date(),
                score=score,
                pnl_30d=data.get("total_pnl"),
                categories_json=data.get("category_strengths"),
            )
            logger.info("Upserted trader %s score=%.3f", data["address"], score)

    logger.info("Discovery complete. Stored %d active traders", len(top))


async def refresh_tracked_traders() -> None:
    """
    Weekly re-score all tracked traders and swap out underperformers.

    Re-scores each active/watching trader. Traders scoring below
    _SCORE_THRESHOLD are deactivated. If active trader count drops
    below _TOP_N, triggers a fresh discovery run.
    """
    logger.info("Refreshing tracked traders")

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        all_traders = await trader_repo.get_all()

    updated_scores: list[tuple[Trader, float]] = []

    for trader in all_traders:
        try:
            async with DataApiClient() as data_client:
                trades = await data_client.get_trades(user=trader.address, limit=200)
            monthly_history = await _build_monthly_pnl_history(trades)
            category_strengths = await compute_category_strengths(trader.address)
            trades_per_month = len(trades) / max(len(monthly_history), 1)

            last_trade_ts: Optional[datetime] = None
            if trades:
                ts_str = trades[0].get("timestamp", trades[0].get("createdAt", ""))
                if ts_str:
                    last_trade_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

            score = compute_composite_score(
                monthly_pnl_history=monthly_history,
                category_strengths=category_strengths,
                trades_per_month=trades_per_month,
                last_active_at=last_trade_ts,
            )
            updated_scores.append((trader, score))
        except Exception as exc:
            logger.warning("Failed to re-score trader %s: %s", trader.address, exc)

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        snapshot_repo = TraderSnapshotRepo(session)
        active_count = 0

        for trader, score in updated_scores:
            new_status = trader.status
            if score < _SCORE_THRESHOLD and trader.status == "active":
                new_status = "watching"
                logger.info(
                    "Downgrading trader %s (score %.3f < threshold %.3f)",
                    trader.address,
                    score,
                    _SCORE_THRESHOLD,
                )
            elif score >= _SCORE_THRESHOLD and trader.status != "inactive":
                new_status = "active"
                active_count += 1

            await trader_repo.update_score(trader.id, score)
            if new_status != trader.status:
                await trader_repo.update_status(trader.id, new_status)

            await snapshot_repo.create(
                trader_id=trader.id,
                date=datetime.now(timezone.utc).date(),
                score=score,
            )

    # If we have too few active traders, run discovery again
    if active_count < _TOP_N // 2:
        logger.info("Too few active traders (%d), running discovery", active_count)
        await discover_top_traders()
    else:
        logger.info("Refresh complete. %d active traders", active_count)
