"""
Trader discovery and scoring engine.

Strategy:
- Pull top 50 from each Polymarket category (10 categories) = ~400 unique candidates
- Score every candidate on consistency, Sharpe, diversity, frequency, recency
- Consistency is weighted 50% — we want steady gainers, not lucky one-hit wonders
- Store top 500 in DB with status='watching'
- Promote top 50 by score to status='active' (these get polled every 30s)
- Weekly refresh: re-score all, rotate active/watching accordingly
"""

import asyncio
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.data_api import DataApiClient
from db.models import Trader
from db.repos.traders import TraderRepo, TraderSnapshotRepo
from db.session import get_session

logger = logging.getLogger(__name__)

_ACTIVE_SCORE_THRESHOLD = 0.55  # Score needed to be actively monitored (polled every 30s)
_WATCHING_N = 500               # Max stored in DB for scoring/reference
_SCORE_THRESHOLD = 0.30         # Minimum score to stay in DB at all
_MIN_TRADES = 30                # Require at least 30 total trades
_MIN_MONTHS_ACTIVE = 3          # Require at least 3 months of history
# No VIP bypass — everyone must pass the same gates. Short history = lucky bet, not a pattern.

# All Polymarket categories to pull from
_CATEGORIES = [
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO",
    "CULTURE", "ECONOMICS", "TECH", "FINANCE",
    "WEATHER",
]

# Time periods to pull from per category
_TIME_PERIODS = ["ALL", "MONTH"]


def _compute_consistency_score(monthly_pnl_history: list[dict]) -> float:
    """
    Consistency = fraction of months with positive net cash flow.
    Uses last 6 months. This is the most important metric.

    A trader with 6/6 profitable months scores 1.0.
    One with 3/6 scores 0.5.
    """
    if not monthly_pnl_history:
        return 0.0
    recent = monthly_pnl_history[-6:]
    if len(recent) < _MIN_MONTHS_ACTIVE:
        # Penalize traders without enough history
        return 0.0
    profitable = sum(1 for m in recent if m.get("pnl", 0) > 0)
    return profitable / max(len(recent), 1)


def _compute_sharpe_normalized(monthly_pnl_history: list[dict]) -> float:
    """
    Normalized Sharpe-like ratio: mean monthly return / std deviation.
    Clamped to [0, 3] then divided by 3 to get [0, 1].
    High Sharpe = consistent gains relative to volatility.
    """
    if len(monthly_pnl_history) < 2:
        return 0.0
    returns = [m.get("pnl", 0.0) for m in monthly_pnl_history]
    mean_ret = statistics.mean(returns)
    try:
        std_ret = statistics.stdev(returns)
    except statistics.StatisticsError:
        return 0.0
    if std_ret == 0:
        return 1.0 if mean_ret > 0 else 0.0
    raw_sharpe = mean_ret / std_ret
    return max(0.0, min(3.0, raw_sharpe)) / 3.0


def _compute_diversity_score(monthly_pnl_history: list[dict], trade_count: int) -> float:
    """
    Diversity proxy: traders with many trades over many months
    are likely diversified across markets.
    Penalizes traders who made 1 giant bet.
    """
    if not monthly_pnl_history or trade_count == 0:
        return 0.0
    months_active = len([m for m in monthly_pnl_history if m.get("pnl", 0) != 0])
    # Average trades per active month — we want people making multiple trades/month
    trades_per_month = trade_count / max(months_active, 1)
    # Score: 10+ trades/month = 1.0, fewer = proportional
    return min(trades_per_month / 10.0, 1.0)


def _compute_frequency_score(trades_per_month: float) -> float:
    """High frequency = actively trading, not just sitting on 1 position."""
    return min(trades_per_month / 10.0, 1.0)


def _compute_recency_multiplier(last_active_at: Optional[datetime]) -> float:
    """
    Recency as a MULTIPLIER (0–1), applied to the entire score.
    A dormant trader is worthless to copy regardless of historical stats.

    - ≤14 days → 1.0 (full score)
    - ≤30 days → 0.7
    - ≤60 days → 0.3
    - >60 days  → 0.0 (hard killed)
    """
    if last_active_at is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)
    days_ago = (now - last_active_at).days
    if days_ago <= 14:
        return 1.0
    if days_ago <= 30:
        return 0.7
    if days_ago <= 60:
        return 0.3
    return 0.0  # >60 days inactive = never copy


def _compute_winning_streak_bonus(monthly_pnl_history: list[dict]) -> float:
    """
    Bonus for recent consecutive profitable months.
    3 months in a row = 0.1 bonus, 6 months = 0.2.
    """
    if not monthly_pnl_history:
        return 0.0
    streak = 0
    for m in reversed(monthly_pnl_history[-6:]):
        if m.get("pnl", 0) > 0:
            streak += 1
        else:
            break
    return min(streak * 0.033, 0.2)  # cap at 0.2


def compute_composite_score(
    monthly_pnl_history: list[dict],
    trade_count: int,
    last_active_at: Optional[datetime],
    weekly_pnl_history: Optional[list] = None,
) -> float:
    """
    Composite trader quality score. Primary metric: weekly win rate.

    Formula:
        base = win_rate_weekly * 0.55     ← dominant: % of weeks profitable
             + recent_form * 0.30         ← last 8 weeks: how many green
             + diversity * 0.15           ← trades across many markets/months
             + streak_bonus (up to 0.15)  ← recent consecutive green weeks
        score = base × recency_multiplier ← kills score for inactive traders
        capped at 1.0

    Hard gates (before scoring):
    - <30 trades total
    - <3 months of history
    - inactive >60 days (recency_mult = 0.0)
    - weekly win rate <40% → score 0 (not worth tracking)

    Args:
        monthly_pnl_history: List of {month: "YYYY-MM", pnl: float} dicts.
        trade_count: Total lifetime trade count.
        last_active_at: Timestamp of last trade.
        weekly_pnl_history: Optional list of {week, pnl} dicts for better win rate calc.

    Returns:
        Composite score in [0, 1].
    """
    # Hard gates
    if trade_count < _MIN_TRADES:
        return 0.0
    if len(monthly_pnl_history) < _MIN_MONTHS_ACTIVE:
        return 0.0

    recency_mult = _compute_recency_multiplier(last_active_at)
    if recency_mult == 0.0:
        return 0.0

    # Weekly win rate — fraction of weeks with positive PnL
    weekly = weekly_pnl_history or []
    if weekly:
        profitable_weeks = sum(1 for w in weekly if w.get("pnl", 0) > 2)
        win_rate = profitable_weeks / len(weekly)
    else:
        # Fallback to monthly consistency if no weekly data
        profitable_months = sum(1 for m in monthly_pnl_history[-6:] if m.get("pnl", 0) > 0)
        win_rate = profitable_months / max(len(monthly_pnl_history[-6:]), 1)

    # Hard gate: <40% win rate = not worth tracking
    if win_rate < 0.40:
        return 0.0

    # Recent form: last 8 weeks
    recent_8 = weekly[-8:] if len(weekly) >= 4 else []
    if recent_8:
        recent_form = sum(1 for w in recent_8 if w.get("pnl", 0) > 2) / len(recent_8)
    else:
        recent_form = win_rate  # Fallback

    # Diversity: active months × trades per month
    months_active = len([m for m in monthly_pnl_history if m.get("pnl", 0) != 0])
    trades_per_month = trade_count / max(months_active, 1)
    diversity = min(trades_per_month / 20.0, 1.0)  # 20+ trades/month = max diversity

    # Streak bonus: consecutive green weeks at the end
    streak = 0
    for w in reversed(weekly[-8:]):
        if w.get("pnl", 0) > 2:
            streak += 1
        else:
            break
    streak_bonus = min(streak * 0.025, 0.15)

    base_score = (
        win_rate * 0.55
        + recent_form * 0.30
        + diversity * 0.15
    )
    score = min((base_score + streak_bonus) * recency_mult, 1.0)

    logger.debug(
        "Score: win_rate=%.2f recent_form=%.2f diversity=%.2f streak=%.2f recency_mult=%.2f → %.3f",
        win_rate, recent_form, diversity, streak_bonus, recency_mult, score,
    )
    return round(score, 4)


def _parse_trade_ts(trade: dict) -> Optional[datetime]:
    """Parse trade timestamp (Unix int or ISO string) to UTC datetime."""
    ts_raw = trade.get("timestamp")
    if not ts_raw:
        return None
    try:
        if isinstance(ts_raw, (int, float)):
            return datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        return datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _compute_trade_cash_flow(trade: dict) -> float:
    size = float(trade.get("size", 0) or 0)
    price = float(trade.get("price", 0) or 0)
    side = trade.get("side", "").upper()
    return size * price if side == "SELL" else -(size * price)


async def _build_monthly_pnl_history(trades: list[dict]) -> list[dict]:
    """
    Aggregate trades into monthly cash-flow records.

    Buys = negative cash flow, Sells = positive.
    This approximates realized PnL from trade activity.
    """
    monthly: dict[str, float] = {}
    for trade in trades:
        ts = _parse_trade_ts(trade)
        if not ts:
            continue
        key = ts.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + _compute_trade_cash_flow(trade)
    return [{"month": k, "pnl": v} for k, v in sorted(monthly.items())]


async def _build_weekly_pnl_history(trades: list[dict]) -> list[dict]:
    """Aggregate trades into weekly cash-flow records (ISO week YYYY-WW)."""
    weekly: dict[str, float] = {}
    for trade in trades:
        ts = _parse_trade_ts(trade)
        if not ts:
            continue
        key = ts.strftime("%Y-%W")
        weekly[key] = weekly.get(key, 0.0) + _compute_trade_cash_flow(trade)
    return [{"week": k, "pnl": v} for k, v in sorted(weekly.items())]


def _compute_extra_stats(
    trades: list[dict],
    weekly_history: list[dict],
) -> dict:
    """
    Compute win_rate, avg_trades_per_week, avg_profit_per_trade from raw trades.

    Returns dict with: win_rate, avg_trades_per_week, avg_profit_per_trade
    """
    if not trades or not weekly_history:
        return {"win_rate": 0.0, "avg_trades_per_week": 0.0, "avg_profit_per_trade": 0.0}

    # Win rate: fraction of weeks with positive net cash flow
    profitable_weeks = sum(1 for w in weekly_history if w.get("pnl", 0) > 0)
    win_rate = profitable_weeks / len(weekly_history) if weekly_history else 0.0

    # Active weeks (weeks with any trades)
    active_weeks = len(weekly_history)
    avg_trades_per_week = len(trades) / max(active_weeks, 1)

    # Avg profit per trade = total net cash flow / trade count
    total_cf = sum(_compute_trade_cash_flow(t) for t in trades)
    avg_profit_per_trade = total_cf / len(trades) if trades else 0.0

    return {
        "win_rate": round(win_rate, 3),
        "avg_trades_per_week": round(avg_trades_per_week, 1),
        "avg_profit_per_trade": round(avg_profit_per_trade, 2),
    }


async def _fetch_all_leaderboard_candidates() -> dict[str, dict]:
    """
    Pull candidates from all categories and time periods.
    Returns a dict keyed by proxyWallet address (deduplicates automatically).
    """
    candidates: dict[str, dict] = {}

    async with DataApiClient() as data_client:
        for category in _CATEGORIES:
            for time_period in _TIME_PERIODS:
                try:
                    # Pull pages: 50 per page, up to 3 pages = 150 per category/period
                    for offset in range(0, 150, 50):
                        entries = await data_client.get_leaderboard(
                            category=category,
                            time_period=time_period,
                            order_by="PNL",
                            limit=50,
                            offset=offset,
                        )
                        if not entries:
                            break
                        for entry in entries:
                            address = entry.get("proxyWallet", "")
                            if not address:
                                continue
                            # Keep the entry with higher PnL if duplicate
                            existing_pnl = candidates.get(address, {}).get("pnl", 0)
                            entry_pnl = float(entry.get("pnl", 0) or 0)
                            if address not in candidates or entry_pnl > existing_pnl:
                                candidates[address] = entry
                        # Small delay to be respectful of API
                        await asyncio.sleep(0.2)
                except Exception as exc:
                    logger.warning(
                        "Leaderboard fetch failed (category=%s, period=%s): %s",
                        category, time_period, exc,
                    )
                    continue

    logger.info("Fetched %d unique candidates from leaderboard", len(candidates))
    return candidates


async def _score_candidate(address: str, entry: dict) -> Optional[tuple[float, dict]]:
    """
    Fetch trade history for a candidate and compute their composite score.

    VIP bypass: traders with leaderboard PnL > $10k AND volume > $100k skip
    the consistency gates and get a base score of 0.5. They're proven in aggregate.

    Returns None if they fail hard gates (too few trades, not enough history).
    """
    leaderboard_rank = int(entry.get("rank", 9999) or 9999)

    try:
        async with DataApiClient() as data_client:
            trades = await data_client.get_all_trades(user=address)

        if len(trades) < _MIN_TRADES:
            return None

        monthly_history = await _build_monthly_pnl_history(trades)
        if len(monthly_history) < _MIN_MONTHS_ACTIVE:
            return None

        weekly_history = await _build_weekly_pnl_history(trades)
        extra_stats = _compute_extra_stats(trades, weekly_history)
        last_trade_ts = _parse_trade_ts(trades[0]) if trades else None

        # Hard gate: inactive traders can't be copied
        if _compute_recency_multiplier(last_trade_ts) == 0.0:
            return None  # >60 days inactive

        score = compute_composite_score(
            monthly_pnl_history=monthly_history,
            trade_count=len(trades),
            last_active_at=last_trade_ts,
            weekly_pnl_history=weekly_history,
        )

        return (score, {
            "address": address,
            "display_name": entry.get("userName", entry.get("name", "")),
            "score": score,
            "total_pnl": leaderboard_pnl,
            "leaderboard_rank": leaderboard_rank,
            "monthly_pnl_history": monthly_history,
            "weekly_pnl_history": weekly_history,
            "trade_count": len(trades),
            "last_active_at": last_trade_ts,
            **extra_stats,
        })
    except Exception as exc:
        logger.debug("Failed to score candidate %s: %s", address, exc)
        return None


async def discover_top_traders() -> None:
    """
    Full trader discovery run.

    1. Pull all leaderboard candidates across categories (up to ~500 unique)
    2. Score each one — heavily weight consistency
    3. Store top _WATCHING_N in DB as 'watching'
    4. Promote top _ACTIVE_SCORE_THRESHOLD to 'active' (these get polled every 30s)
    5. Downgrade existing active traders not in new top set
    """
    logger.info("Starting full trader discovery (score_threshold=%.2f, watching_cap=%d)", _ACTIVE_SCORE_THRESHOLD, _WATCHING_N)

    candidates = await _fetch_all_leaderboard_candidates()
    if not candidates:
        logger.warning("No candidates found — aborting discovery")
        return

    # Score all candidates (with concurrency limit to avoid rate limits)
    logger.info("Scoring %d candidates...", len(candidates))
    scored: list[tuple[float, dict]] = []
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent trade fetches

    async def score_with_sem(address: str, entry: dict) -> None:
        async with semaphore:
            result = await _score_candidate(address, entry)
            if result is not None:
                scored.append(result)
            await asyncio.sleep(0.1)  # Rate limit courtesy

    tasks = [score_with_sem(addr, entry) for addr, entry in candidates.items()]
    await asyncio.gather(*tasks)

    logger.info(
        "Scored %d/%d candidates passed hard gates (min %d trades, %d months)",
        len(scored), len(candidates), _MIN_TRADES, _MIN_MONTHS_ACTIVE,
    )

    if not scored:
        logger.warning("No candidates passed scoring gates")
        return

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    top_watching = scored[:_WATCHING_N]
    # All traders above threshold become active — no arbitrary cap
    top_active_addresses = {d["address"] for s, d in top_watching if s >= _ACTIVE_SCORE_THRESHOLD}
    top_watching_addresses = {d["address"] for _, d in top_watching}
    logger.info(
        "%d traders scored above active threshold (%.2f), %d total in watching pool",
        len(top_active_addresses), _ACTIVE_SCORE_THRESHOLD, len(top_watching),
    )

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        snapshot_repo = TraderSnapshotRepo(session)

        # Downgrade existing active/watching traders no longer in our sets
        existing = await trader_repo.get_all()
        for t in existing:
            if t.address not in top_watching_addresses and t.status != "inactive":
                if t.is_pinned:
                    logger.info("Skipping downgrade of pinned trader %s", t.address)
                else:
                    logger.info("Downgrading trader %s to inactive (dropped from ranking)", t.address)
                    await trader_repo.update_status(t.id, "inactive")

        # Upsert all top traders
        for score, data in top_watching:
            new_status = "active" if data["address"] in top_active_addresses else "watching"
            trader = await trader_repo.upsert(
                address=data["address"],
                display_name=data.get("display_name"),
                score=score,
                status=new_status,
                category_strengths=None,
                total_pnl=data.get("total_pnl", 0.0),
                leaderboard_rank=data.get("leaderboard_rank"),
                monthly_pnl_history=data.get("monthly_pnl_history"),
                weekly_pnl_history=data.get("weekly_pnl_history"),
                trade_count=data.get("trade_count", 0),
                win_rate=data.get("win_rate"),
                avg_trades_per_week=data.get("avg_trades_per_week"),
                avg_profit_per_trade=data.get("avg_profit_per_trade"),
                last_active_at=data.get("last_active_at"),
            )
            await snapshot_repo.create(
                trader_id=trader.id,
                date=datetime.now(timezone.utc).date(),
                score=score,
                pnl_30d=data.get("total_pnl"),
                categories_json=None,
            )

    active_count = sum(1 for s, d in top_watching if d["address"] in top_active_addresses)
    watching_count = len(top_watching) - active_count
    logger.info(
        "Discovery complete: %d active (polled every 30s), %d watching (scored, not polled)",
        active_count, watching_count,
    )

    # Log top 20 for visibility
    for i, (score, data) in enumerate(scored[:20], 1):
        name = data.get("display_name") or data["address"][:12]
        status = "ACTIVE" if data["address"] in top_active_addresses else "watching"
        logger.info(
            "  #%d [%s] %s — score=%.3f trades=%d consistency=%s",
            i, status, name, score, data["trade_count"],
            f"{sum(1 for m in data['monthly_pnl_history'][-6:] if m.get('pnl',0)>0)}/6mo"
            if data.get("monthly_pnl_history") else "?",
        )


async def refresh_tracked_traders() -> None:
    """
    Weekly re-score all tracked traders and rotate active/watching.

    Re-scores each trader, promotes/demotes based on current performance.
    Runs a fresh discovery if active count drops below half of _ACTIVE_SCORE_THRESHOLD.
    """
    logger.info("Refreshing tracked traders")

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        all_traders = await trader_repo.get_all()

    updated: list[tuple[Trader, float]] = []

    async def rescore(trader: Trader) -> None:
        try:
            async with DataApiClient() as data_client:
                trades = await data_client.get_all_trades(user=trader.address)
            if len(trades) < _MIN_TRADES:
                updated.append((trader, 0.0))
                return

            monthly_history = await _build_monthly_pnl_history(trades)
            last_trade_ts: Optional[datetime] = None
            if trades:
                ts_raw = trades[0].get("timestamp")
                if ts_raw:
                    if isinstance(ts_raw, (int, float)):
                        last_trade_ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                    else:
                        last_trade_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

            score = compute_composite_score(
                monthly_pnl_history=monthly_history,
                trade_count=len(trades),
                last_active_at=last_trade_ts,
                weekly_pnl_history=weekly_history,
            )
            updated.append((trader, score))
        except Exception as exc:
            logger.warning("Failed to re-score %s: %s", trader.address, exc)

    semaphore = asyncio.Semaphore(5)

    async def rescore_with_sem(t: Trader) -> None:
        async with semaphore:
            await rescore(t)
            await asyncio.sleep(0.1)

    await asyncio.gather(*[rescore_with_sem(t) for t in all_traders])

    # Sort by new score, assign statuses
    updated.sort(key=lambda x: x[1], reverse=True)
    # Active = all above threshold (no arbitrary cap)
    active_addresses = {t.address for t, s in updated if s >= _ACTIVE_SCORE_THRESHOLD}
    watching_addresses = {t.address for t, s in updated[:_WATCHING_N] if t.address not in active_addresses}

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        snapshot_repo = TraderSnapshotRepo(session)
        active_count = 0

        for trader, score in updated:
            if trader.address in active_addresses and score >= _SCORE_THRESHOLD:
                new_status = "active"
                active_count += 1
            elif trader.address in watching_addresses:
                new_status = "watching"
            else:
                new_status = "inactive"

            await trader_repo.update_score(trader.id, score)
            if new_status != trader.status:
                logger.info(
                    "Trader %s: %s → %s (score=%.3f)",
                    trader.address[:12], trader.status, new_status, score,
                )
                await trader_repo.update_status(trader.id, new_status)

            await snapshot_repo.create(
                trader_id=trader.id,
                date=datetime.now(timezone.utc).date(),
                score=score,
            )

    if active_count < 10:
        logger.info("Active traders dropped to %d — triggering fresh discovery", active_count)
        await discover_top_traders()
    else:
        logger.info("Refresh complete: %d active, %d total tracked", active_count, len(updated))
