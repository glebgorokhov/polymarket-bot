"""
Trader discovery and scoring engine.

Strategy:
- Pull candidates from Polymarket leaderboard across all categories
- Score using ground-truth data: leaderboard PnL + positions-derived metrics
- Forget weekly cash flow / win rate — both are broken for hold-to-resolution traders
  (CTF contract payouts bypass the trades API entirely)
- Real scoring:
    * Leaderboard PnL = real profit (ground truth)
    * Active duration from positions (oldest endDate to today)
    * Capital efficiency = leaderboard_pnl / volume
    * Recent activity (last position or trade within 14 days)
    * Position sizing consistency (avg bet size as % of implied portfolio)
- Store top _WATCHING_N in DB as 'watching'
- Promote to 'active' those above _ACTIVE_SCORE_THRESHOLD (polled every 30s)
"""

import asyncio
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.data_api import DataApiClient
from db.models import Trader
from db.repos.traders import TraderPnlSnapshotRepo, TraderRepo, TraderSnapshotRepo
from db.session import get_session

logger = logging.getLogger(__name__)

_ACTIVE_SCORE_THRESHOLD = 0.45  # Score to be actively monitored
_WATCHING_N = 500               # Max stored in DB
_SCORE_THRESHOLD = 0.20         # Minimum score to stay in DB at all

# Hard gates
_MIN_LEADERBOARD_PNL = 500.0    # Must have made real money (leaderboard ground truth)
_MIN_ACTIVE_DAYS = 30           # Must have been active for at least 30 days
_MIN_EFFICIENCY = 0.01          # PnL/Volume must be ≥ 1% (some edge, not random)
_MAX_INACTIVE_DAYS = 21         # Must have traded in the last 21 days
_MIN_POSITIONS = 10             # Need at least 10 positions to score meaningfully

# All Polymarket categories to pull from
_CATEGORIES = [
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO",
    "CULTURE", "ECONOMICS", "TECH", "FINANCE",
    "WEATHER",
]

# Time periods to pull from per category
_TIME_PERIODS = ["ALL", "MONTH"]


def compute_composite_score(
    leaderboard_pnl: float,
    leaderboard_volume: float,
    active_days: int,
    last_active_days_ago: int,
    position_count: int,
    avg_position_size: float,
    curve_consistency: Optional[float] = None,
) -> float:
    """
    Ground-truth composite score. Uses leaderboard data as the primary signal.

    Score components (all 0–1):
    1. Profitability (40%): PnL normalized against top earners
    2. Efficiency (30%): PnL/Volume — how much they make per dollar bet
    3. Duration (20%): how long they've been active (longer = more proven)
    4. Activity (10%): how recently they traded (staleness penalty)

    Hard gates applied before calling this:
    - leaderboard_pnl < _MIN_LEADERBOARD_PNL → 0
    - active_days < _MIN_ACTIVE_DAYS → 0
    - efficiency < _MIN_EFFICIENCY → 0
    - last_active_days_ago > _MAX_INACTIVE_DAYS → 0
    """
    # Component 1: Profitability — log scale, $1k=0.3, $10k=0.6, $50k=0.85, $200k=1.0
    if leaderboard_pnl <= 0:
        return 0.0
    import math
    pnl_score = min(math.log10(leaderboard_pnl) / math.log10(200_000), 1.0)

    # Component 2: Capital efficiency — 1%=0.1, 5%=0.5, 10%=1.0
    efficiency = leaderboard_pnl / max(leaderboard_volume, 1)
    efficiency_score = min(efficiency / 0.10, 1.0)

    # Component 3: Duration — 30d=0.25, 90d=0.5, 180d=0.75, 365d=1.0
    duration_score = min(active_days / 365, 1.0)

    # Component 4: Recency — full score within 7 days, drops to 0 at 21 days
    recency_score = max(0.0, 1.0 - (last_active_days_ago / _MAX_INACTIVE_DAYS))

    if curve_consistency is not None:
        # We have snapshot-based curve data — incorporate it
        # Curve consistency replaces some of the duration/recency weight
        # because it directly measures what we care about (smooth growth)
        score = (
            pnl_score * 0.35
            + efficiency_score * 0.25
            + curve_consistency * 0.25  # direct curve quality measurement
            + duration_score * 0.10
            + recency_score * 0.05
        )
    else:
        # No snapshot data yet — use proxy metrics only
        score = (
            pnl_score * 0.40
            + efficiency_score * 0.30
            + duration_score * 0.20
            + recency_score * 0.10
        )

    return round(min(score, 1.0), 4)


def _compute_position_win_rate(positions: list[dict]) -> float:
    """
    Attempt to compute win rate from positions API.

    IMPORTANT: This metric is unreliable for hold-to-resolution traders.
    When a position resolves through CTF contract payout, cashPnl shows -initialValue
    regardless of whether the trade was profitable. Only use as informational stat,
    not as a hard gate.
    """
    wins = 0
    losses = 0
    for p in positions:
        current_value = float(p.get("currentValue", 0) or 0)
        cash_pnl = float(p.get("cashPnl", 0) or 0)
        if current_value < 1.0:
            # Only count if they explicitly sold (both buy and sell appear in trades)
            realized = float(p.get("realizedPnl", 0) or 0)
            if realized > 0.01:  # has a positive realized sale
                wins += 1
            elif realized < -0.01:
                losses += 1
    total = wins + losses
    if total < 5:
        return -1.0  # not enough explicit sells to compute
    return round(wins / total, 3)


async def _fetch_all_leaderboard_candidates() -> dict[str, dict]:
    """
    Pull trader candidates from two sources:

    1. Leaderboard (top ~200 per category × 9 categories = ~500 unique)
       These are the richest traders by PnL — well-known, proven.

    2. Top-50 markets by volume (market-level participant discovery)
       Each high-volume market has hundreds to thousands of traders.
       Many are NOT on the global leaderboard but trade specific domains well.
       This is how we find small, consistent traders with perfect curve consistency.

    Returns dict of address → leaderboard entry dict (with pnl, vol, rank fields).
    For market-discovered traders without a leaderboard entry, pnl/vol default to 0
    and get filled in during _score_candidate via actual positions + leaderboard lookup.
    """
    candidates: dict[str, dict] = {}

    # Source 1: Leaderboard
    async with DataApiClient() as client:
        for category in _CATEGORIES:
            try:
                entries = await client.get_leaderboard(
                    category=category,
                    order_by="PNL",
                    limit=200,
                )
                for entry in entries:
                    address = (entry.get("proxyWallet") or entry.get("address") or "").lower()
                    if not address:
                        continue
                    existing_pnl = float(candidates.get(address, {}).get("pnl", 0))
                    entry_pnl = float(entry.get("pnl", 0) or 0)
                    if address not in candidates or entry_pnl > existing_pnl:
                        candidates[address] = entry
                await asyncio.sleep(0.2)
            except Exception as exc:
                logger.warning("Leaderboard fetch failed (category=%s): %s", category, exc)

    logger.info("Leaderboard: %d unique candidates", len(candidates))

    # Source 2: Top markets by volume
    # Fetch trader addresses from each market's trade history
    # Market-level trades have NO offset cap (unlike user-level 3000 cap)
    try:
        async with DataApiClient() as client:
            markets = await client.get_top_markets(limit=50)

        market_trader_count = 0
        sem = asyncio.Semaphore(3)

        async def collect_market_traders(market: dict) -> None:
            nonlocal market_trader_count
            cid = market.get("conditionId", "")
            if not cid:
                return
            async with sem:
                try:
                    async with DataApiClient() as client:
                        traders = await client.get_all_traders_in_market(cid)
                    for addr in traders:
                        if addr not in candidates:
                            candidates[addr] = {"pnl": 0, "vol": 0, "rank": 9999, "proxyWallet": addr}
                            market_trader_count += 1
                    await asyncio.sleep(0.3)
                except Exception as exc:
                    logger.debug("Market %s trader fetch failed: %s", cid[:16], exc)

        await asyncio.gather(*[collect_market_traders(m) for m in markets[:50]])
        logger.info("Markets: added %d new traders not on leaderboard", market_trader_count)

    except Exception as exc:
        logger.warning("Market-level discovery failed: %s", exc)

    logger.info("Total candidates: %d (leaderboard + market-level)", len(candidates))
    return candidates


async def _score_candidate(address: str, entry: dict) -> Optional[tuple[float, dict]]:
    """
    Score a candidate using ground-truth leaderboard + positions data.

    Core insight: leaderboard PnL is computed by Polymarket and accounts for
    all payouts including CTF contract resolutions. It's the only reliable
    profitability signal we have.

    Positions are used for:
    - Active duration (oldest position endDate)
    - Average position size (bet sizing behavior)
    - Position count (trading frequency)
    """
    leaderboard_pnl = float(entry.get("pnl", 0) or 0)
    leaderboard_volume = float(entry.get("vol", 0) or 0)
    leaderboard_rank = int(entry.get("rank", 9999) or 9999)

    # Gate 1: Must have made real money
    if leaderboard_pnl < _MIN_LEADERBOARD_PNL:
        return None

    # Gate 2: Must have some edge (not just volume)
    efficiency = leaderboard_pnl / max(leaderboard_volume, 1)
    if efficiency < _MIN_EFFICIENCY:
        return None

    try:
        async with DataApiClient() as data_client:
            positions = await data_client.get_positions(user=address)

        if len(positions) < _MIN_POSITIONS:
            return None

        # Derive active duration from positions (oldest endDate)
        end_dates = []
        for p in positions:
            ed = p.get("endDate")
            if ed:
                try:
                    end_dates.append(datetime.fromisoformat(ed.replace("Z", "+00:00")))
                except Exception:
                    pass

        now = datetime.now(timezone.utc)
        if end_dates:
            oldest_date = min(end_dates)
            active_days = (now - oldest_date).days
        else:
            active_days = 0

        if active_days < _MIN_ACTIVE_DAYS:
            return None

        # Last activity: check most recent position endDate that's in the past
        past_end_dates = [d for d in end_dates if d <= now]
        if past_end_dates:
            last_active = max(past_end_dates)
            last_active_days_ago = (now - last_active).days
        else:
            last_active_days_ago = 999

        # Also try trades API for more recent activity signal
        try:
            async with DataApiClient() as data_client:
                recent_trades = await data_client.get_trades(user=address, limit=5)
            if recent_trades:
                last_ts = recent_trades[0].get("timestamp")
                if last_ts:
                    if isinstance(last_ts, (int, float)):
                        last_trade_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc)
                    else:
                        last_trade_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                    trade_days_ago = (now - last_trade_dt).days
                    last_active_days_ago = min(last_active_days_ago, trade_days_ago)
        except Exception:
            pass

        if last_active_days_ago > _MAX_INACTIVE_DAYS:
            return None

        # Position sizing behavior
        initial_values = [float(p.get("initialValue", 0) or 0) for p in positions if float(p.get("initialValue", 0) or 0) > 0]
        avg_position_size = statistics.mean(initial_values) if initial_values else 0
        median_position_size = statistics.median(initial_values) if initial_values else 0

        # Implied portfolio estimate: leaderboard PnL + avg open position value
        open_value = sum(float(p.get("currentValue", 0) or 0) for p in positions if float(p.get("currentValue", 0) or 0) > 0)
        implied_portfolio = leaderboard_pnl + open_value  # rough estimate
        avg_bet_pct = avg_position_size / max(implied_portfolio, 1)

        # Look up stored curve consistency (from previous discovery snapshots)
        curve_consistency: Optional[float] = None
        try:
            async with get_session() as snap_session:
                snap_repo = TraderPnlSnapshotRepo(snap_session)
                curve_consistency = await snap_repo.compute_curve_consistency(address)
        except Exception:
            pass

        # Compute score
        score = compute_composite_score(
            leaderboard_pnl=leaderboard_pnl,
            leaderboard_volume=leaderboard_volume,
            active_days=active_days,
            last_active_days_ago=last_active_days_ago,
            position_count=len(positions),
            avg_position_size=avg_position_size,
            curve_consistency=curve_consistency,
        )

        if score < _SCORE_THRESHOLD:
            return None

        # Best-effort win rate (informational only — unreliable for hold-to-resolution)
        win_rate = _compute_position_win_rate(positions)

        return (score, {
            "address": address,
            "display_name": entry.get("userName", entry.get("name", "")),
            "score": score,
            "total_pnl": leaderboard_pnl,
            "leaderboard_rank": leaderboard_rank,
            "trade_count": len(positions),
            "avg_trades_per_week": round(len(positions) / max(active_days / 7, 1), 1),
            "win_rate": max(win_rate, 0) if win_rate >= 0 else None,
            "avg_profit_per_trade": leaderboard_pnl / max(len(positions), 1),
            "last_active_at": now - timedelta(days=last_active_days_ago),
            "monthly_pnl_history": [],
            "weekly_pnl_history": [],
            # Extra stats stored in category_strengths JSONB column (repurposed)
            "category_strengths": {
                "avg_bet_pct": round(avg_bet_pct * 100, 2),
                "avg_position_size": round(avg_position_size, 2),
                "median_position_size": round(median_position_size, 2),
                "implied_portfolio": round(implied_portfolio, 2),
                "open_position_value": round(open_value, 2),
                "active_days": active_days,
                "efficiency_pct": round(efficiency * 100, 2),
            },
        })

    except Exception as exc:
        logger.debug("Failed to score candidate %s: %s", address, exc)
        return None


async def discover_top_traders() -> None:
    """
    Full trader discovery run.

    1. Pull all leaderboard candidates across categories (up to ~500 unique)
    2. Score each using leaderboard PnL + positions data
    3. Store top _WATCHING_N in DB as 'watching'
    4. Promote those above _ACTIVE_SCORE_THRESHOLD to 'active' (polled every 30s)
    5. Downgrade existing active traders not in new top set
    """
    logger.info(
        "Starting trader discovery (active_threshold=%.2f, watching_cap=%d)",
        _ACTIVE_SCORE_THRESHOLD, _WATCHING_N,
    )

    candidates = await _fetch_all_leaderboard_candidates()
    if not candidates:
        logger.warning("No candidates found — aborting discovery")
        return

    logger.info("Scoring %d candidates...", len(candidates))
    scored: list[tuple[float, dict]] = []
    semaphore = asyncio.Semaphore(5)

    async def score_with_sem(address: str, entry: dict) -> None:
        async with semaphore:
            result = await _score_candidate(address, entry)
            if result is not None:
                scored.append(result)
            await asyncio.sleep(0.1)

    tasks = [score_with_sem(addr, entry) for addr, entry in candidates.items()]
    await asyncio.gather(*tasks)

    logger.info(
        "%d/%d candidates passed gates (min PnL=$%.0f, efficiency≥%.0f%%, active≤%dd)",
        len(scored), len(candidates),
        _MIN_LEADERBOARD_PNL, _MIN_EFFICIENCY * 100, _MAX_INACTIVE_DAYS,
    )

    if not scored:
        logger.warning("No traders passed scoring gates")
        return

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    top_watching = scored[:_WATCHING_N]

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        snapshot_repo = TraderPnlSnapshotRepo(session)

        # Load all existing traders (including pinned)
        existing_traders: dict[str, Trader] = {
            t.address: t for t in await trader_repo.get_all()
        }

        new_addresses = set()
        for _score, data in top_watching:
            address = data["address"]
            new_addresses.add(address)
            # Store PnL snapshot for curve consistency tracking
            await snapshot_repo.add(
                address=address,
                leaderboard_pnl=data.get("total_pnl", 0),
                leaderboard_volume=None,
                leaderboard_rank=data.get("leaderboard_rank"),
                open_position_value=data.get("category_strengths", {}).get("open_position_value"),
            )

            # Determine status
            rank_in_scored = next(
                (i for i, (_, d) in enumerate(scored) if d["address"] == address), 9999
            )
            new_status = "active" if data["score"] >= _ACTIVE_SCORE_THRESHOLD else "watching"

            existing = existing_traders.get(address)
            if existing:
                # Never downgrade pinned traders
                if existing.is_pinned:
                    new_status = "active"
                await trader_repo.upsert(address, {**data, "status": new_status})
            else:
                await trader_repo.upsert(address, {**data, "status": new_status})

        # Downgrade traders no longer in top set (unless pinned)
        for address, trader in existing_traders.items():
            if address not in new_addresses and trader.status == "active":
                if trader.is_pinned:
                    logger.info("Pinned trader %s not in new top set — keeping active", address[:16])
                    continue
                logger.info("Downgrading %s → watching (dropped from top set)", address[:16])
                await trader_repo.upsert(address, {"status": "watching"})

        active_count = sum(1 for s, d in top_watching if d["score"] >= _ACTIVE_SCORE_THRESHOLD)
        logger.info(
            "Discovery complete: %d active, %d watching, %d total in DB",
            active_count, len(top_watching) - active_count, len(top_watching),
        )

        await session.commit()


async def refresh_trader_scores() -> None:
    """
    Re-score all existing traders in DB using latest leaderboard + positions data.
    Run weekly to rotate active/watching based on updated performance.
    """
    async with get_session() as session:
        trader_repo = TraderRepo(session)
        traders = list(await trader_repo.get_all())

    logger.info("Refreshing scores for %d traders", len(traders))

    async with DataApiClient() as data_client:
        leaderboard_entries = await data_client.get_leaderboard(
            category="OVERALL", order_by="PNL", limit=500
        )

    # Build address → entry map from leaderboard
    lb_map: dict[str, dict] = {}
    for entry in leaderboard_entries:
        addr = (entry.get("proxyWallet") or "").lower()
        if addr:
            lb_map[addr] = entry

    semaphore = asyncio.Semaphore(5)
    updates: list[tuple[str, float, dict]] = []

    async def refresh_one(trader: Trader) -> None:
        async with semaphore:
            lb_entry = lb_map.get(trader.address.lower())
            if not lb_entry:
                logger.debug("Trader %s not on leaderboard anymore", trader.address[:16])
                return
            result = await _score_candidate(trader.address, lb_entry)
            if result:
                updates.append((trader.address, result[0], result[1]))
            await asyncio.sleep(0.1)

    await asyncio.gather(*[refresh_one(t) for t in traders])

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        for address, score, data in updates:
            existing = await trader_repo.get_by_address(address)
            if existing and existing.is_pinned:
                data["status"] = "active"
            else:
                data["status"] = "active" if score >= _ACTIVE_SCORE_THRESHOLD else "watching"
            await trader_repo.upsert(address, data)
        await session.commit()

    logger.info("Refreshed %d trader scores", len(updates))


def _parse_trade_ts(trade: dict) -> Optional[datetime]:
    """Parse trade timestamp to datetime."""
    ts = trade.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
