"""Copytrading backtester with strategy + sizing comparison.

Simulates multiple strategies and sizing models against all tracked traders'
historical trade data. Produces per-trader results AND aggregate strategy
comparison tables.

Strategies:
  pure_follow  — copy every trade from every tracked trader
  whale        — only trades where the trader bet ≥5% of their portfolio
  consensus    — only when 2+ tracked traders bet same outcome on same market
                 (within CONSENSUS_WINDOW_HOURS)
  recency      — only trades from the last RECENCY_DAYS days

Sizing models:
  proportional — scale their bet proportionally to our budget
                 our_bet = min(their_bet/implied_portfolio, 20%) * budget
  fixed        — equal flat bet per signal (budget / FIXED_TRADES_EXPECTED)
  conviction   — proportional × (1 + conviction_bonus)
                 conviction = how far the entry price is from 0.5
                 (cheap options = higher conviction bonus)

Usage:
    async with DataApiClient() as dc:
        results = await run_full_simulation(traders, dc, budget=50.0)
    # results.per_trader   — {address: TraderResult}
    # results.strategies   — {strategy: AggregateResult}
    # results.sizing       — {sizing: AggregateResult}
    # results.matrix       — {(strategy, sizing): AggregateResult}
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_BATCH_SIZE = 20

# Strategy parameters
WHALE_THRESHOLD = 0.05          # ≥5% of implied portfolio
CONSENSUS_WINDOW_HOURS = 24     # trades within 24h count as consensus
RECENCY_DAYS = 60               # for recency filter
FIXED_TRADES_EXPECTED = 25      # fixed sizing: budget / this = bet per signal
MAX_BET_PCT = 0.20              # cap: never bet >20% of budget on one trade
CONVICTION_MAX_BONUS = 0.5      # max 50% extra for highly-priced conviction bets


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    condition_id: str
    title: str
    closed: bool
    outcome_prices: list[float]


@dataclass
class TradeRecord:
    """Normalised trade from data API."""
    condition_id: str
    title: str
    trader_address: str
    side: str             # BUY / SELL
    outcome_index: int
    outcome: str          # "YES" / team name
    price: float          # 0–1
    size: float           # shares
    cost: float           # price * size (USDC)
    timestamp: int        # unix


@dataclass
class MarketPosition:
    """Aggregated position for one trader in one market."""
    condition_id: str
    title: str
    trader_address: str
    outcome_index: int
    outcome: str
    total_cost: float
    total_received_sells: float
    remaining_shares: float
    avg_price: float
    first_trade_ts: int
    implied_portfolio: float     # trader's implied portfolio at trade time


@dataclass
class BetResult:
    """Outcome of one simulated bet."""
    condition_id: str
    title: str
    trader_address: str
    strategy: str
    sizing: str
    our_cost: float
    our_pnl: float
    won: bool


@dataclass
class AggregateResult:
    """Strategy/sizing aggregate across all traders and all bets."""
    strategy: str
    sizing: str
    budget: float
    our_pnl: float
    our_pnl_pct: float
    total_bets: int
    won_bets: int
    lost_bets: int

    @property
    def win_rate(self) -> float:
        if self.total_bets == 0:
            return 0.0
        return self.won_bets / self.total_bets


@dataclass
class TraderResult:
    """Per-trader simulation result (pure_follow / proportional sizing)."""
    trader_address: str
    display_name: Optional[str]
    budget: float
    our_pnl: float
    our_pnl_pct: float
    simulated_days: int
    total_markets: int
    won_markets: int
    lost_markets: int
    open_markets: int
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FullSimResult:
    per_trader: dict[str, TraderResult]
    strategies: dict[str, AggregateResult]    # {strategy_name: result}  (proportional sizing)
    sizing: dict[str, AggregateResult]        # {sizing_name: result}    (pure_follow strategy)
    matrix: dict[tuple[str, str], AggregateResult]  # {(strategy, sizing): result}
    budget: float
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Gamma API
# ---------------------------------------------------------------------------

async def _fetch_market_batch(condition_ids: list[str]) -> list[dict]:
    if not condition_ids:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            params = [("conditionIds", cid) for cid in condition_ids]
            resp = await client.get(f"{_GAMMA_BASE}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("gamma batch fetch failed: %s", exc)
        return []


async def get_market_info(condition_ids: list[str]) -> dict[str, MarketInfo]:
    results: dict[str, MarketInfo] = {}
    for i in range(0, len(condition_ids), _BATCH_SIZE):
        batch = condition_ids[i : i + _BATCH_SIZE]
        markets = await _fetch_market_batch(batch)
        for m in markets:
            cid = m.get("conditionId", "")
            if not cid:
                continue
            closed = bool(m.get("closed", False)) and not bool(m.get("active", True))
            raw_prices = m.get("outcomePrices", [])
            prices: list[float] = []
            for p in raw_prices:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    prices.append(0.0)
            results[cid] = MarketInfo(
                condition_id=cid,
                title=m.get("question") or m.get("title") or "?",
                closed=closed,
                outcome_prices=prices,
            )
        if len(condition_ids) > _BATCH_SIZE:
            await asyncio.sleep(0.15)
    return results


# ---------------------------------------------------------------------------
# Normalise raw trades
# ---------------------------------------------------------------------------

def _parse_trades(raw: list[dict], trader_address: str) -> list[TradeRecord]:
    records: list[TradeRecord] = []
    for t in raw:
        try:
            cid = t.get("conditionId", "")
            if not cid:
                continue
            side = (t.get("side") or "BUY").upper()
            price = float(t.get("price", 0) or 0)
            size = float(t.get("size", 0) or 0)
            if size <= 0:
                continue
            records.append(TradeRecord(
                condition_id=cid,
                title=t.get("title") or t.get("slug") or "?",
                trader_address=trader_address,
                side=side,
                outcome_index=int(t.get("outcomeIndex", 0) or 0),
                outcome=t.get("outcome") or "?",
                price=price,
                size=size,
                cost=price * size,
                timestamp=int(t.get("timestamp", 0) or 0),
            ))
        except Exception:
            continue
    return records


def _build_positions(
    trades: list[TradeRecord], implied_portfolio: float
) -> list[MarketPosition]:
    """Aggregate trades per conditionId into positions."""
    by_market: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_market[t.condition_id].append(t)

    positions: list[MarketPosition] = []
    for cid, mkt_trades in by_market.items():
        buys = [t for t in mkt_trades if t.side == "BUY"]
        sells = [t for t in mkt_trades if t.side == "SELL"]
        if not buys:
            continue
        total_cost = sum(t.cost for t in buys)
        total_recv = sum(t.cost for t in sells)
        total_bought = sum(t.size for t in buys)
        total_sold = sum(t.size for t in sells)
        remaining = max(0.0, total_bought - total_sold)
        avg_price = total_cost / total_bought if total_bought > 0 else 0
        first_ts = min(t.timestamp for t in buys)
        # Use first buy's outcome (all buys in same market should be same side)
        oi = buys[0].outcome_index
        outcome = buys[0].outcome
        title = buys[0].title

        positions.append(MarketPosition(
            condition_id=cid,
            title=title,
            trader_address=buys[0].trader_address,
            outcome_index=oi,
            outcome=outcome,
            total_cost=total_cost,
            total_received_sells=total_recv,
            remaining_shares=remaining,
            avg_price=avg_price,
            first_trade_ts=first_ts,
            implied_portfolio=implied_portfolio,
        ))
    return positions


# ---------------------------------------------------------------------------
# Sizing models
# ---------------------------------------------------------------------------

def _size_proportional(pos: MarketPosition, budget: float) -> float:
    """Scale bet proportionally to trader's portfolio allocation."""
    if pos.implied_portfolio <= 0:
        return budget * 0.05  # fallback: 5%
    pct = pos.total_cost / pos.implied_portfolio
    return min(pct, MAX_BET_PCT) * budget


def _size_fixed(pos: MarketPosition, budget: float) -> float:
    """Equal flat bet per signal regardless of position size."""
    return budget / FIXED_TRADES_EXPECTED


def _size_conviction(pos: MarketPosition, budget: float) -> float:
    """Proportional sizing boosted by entry price distance from 0.5.
    
    Low-priced entries (e.g., 0.10) have higher potential return — bonus.
    High-priced entries near 0.5 are less certain — no bonus.
    """
    base = _size_proportional(pos, budget)
    # Distance from 0.5: [0,0.5] maps to [0, MAX_BONUS]
    distance = abs(0.5 - pos.avg_price)
    bonus = (distance / 0.5) * CONVICTION_MAX_BONUS
    return min(base * (1 + bonus), MAX_BET_PCT * budget)


_SIZERS = {
    "proportional": _size_proportional,
    "fixed": _size_fixed,
    "conviction": _size_conviction,
}


# ---------------------------------------------------------------------------
# Strategy filters
# ---------------------------------------------------------------------------

def _filter_pure_follow(
    pos: MarketPosition,
    all_positions: list[MarketPosition],
    now_ts: int,
) -> bool:
    return True


def _filter_whale(
    pos: MarketPosition,
    all_positions: list[MarketPosition],
    now_ts: int,
) -> bool:
    if pos.implied_portfolio <= 0:
        return False
    return (pos.total_cost / pos.implied_portfolio) >= WHALE_THRESHOLD


def _filter_consensus(
    pos: MarketPosition,
    all_positions: list[MarketPosition],
    now_ts: int,
) -> bool:
    """True if 2+ different traders bet the same outcome on this market
    within CONSENSUS_WINDOW_HOURS of this position's first trade."""
    window = CONSENSUS_WINDOW_HOURS * 3600
    same_side = [
        p for p in all_positions
        if p.condition_id == pos.condition_id
        and p.outcome_index == pos.outcome_index
        and p.trader_address != pos.trader_address
        and abs(p.first_trade_ts - pos.first_trade_ts) <= window
    ]
    return len(same_side) >= 1  # this + at least 1 other = consensus


def _filter_recency(
    pos: MarketPosition,
    all_positions: list[MarketPosition],
    now_ts: int,
) -> bool:
    cutoff = now_ts - RECENCY_DAYS * 86400
    return pos.first_trade_ts >= cutoff


_FILTERS = {
    "pure_follow": _filter_pure_follow,
    "whale": _filter_whale,
    "consensus": _filter_consensus,
    "recency": _filter_recency,
}


# ---------------------------------------------------------------------------
# Simulate one position → BetResult
# ---------------------------------------------------------------------------

def _simulate_position(
    pos: MarketPosition,
    mi: MarketInfo,
    strategy: str,
    sizing: str,
    budget: float,
    all_positions: list[MarketPosition],
    now_ts: int,
) -> Optional[BetResult]:
    """Return BetResult if position passes strategy filter, else None."""
    filt = _FILTERS[strategy]
    if not filt(pos, all_positions, now_ts):
        return None

    sizer = _SIZERS[sizing]
    our_cost = sizer(pos, budget)
    if our_cost <= 0:
        return None

    # Payout from remaining shares (held to resolution)
    if pos.remaining_shares > 0 and mi.outcome_prices and pos.outcome_index < len(mi.outcome_prices):
        payout_per_share = mi.outcome_prices[pos.outcome_index]
        resolution_payout = pos.remaining_shares * payout_per_share
    else:
        resolution_payout = 0.0

    trader_net = pos.total_received_sells + resolution_payout - pos.total_cost
    if pos.total_cost <= 0:
        return None

    our_pnl = (trader_net / pos.total_cost) * our_cost

    return BetResult(
        condition_id=pos.condition_id,
        title=pos.title[:50],
        trader_address=pos.trader_address,
        strategy=strategy,
        sizing=sizing,
        our_cost=our_cost,
        our_pnl=our_pnl,
        won=(trader_net > 0),
    )


# ---------------------------------------------------------------------------
# Full simulation engine
# ---------------------------------------------------------------------------

class FullBacktestEngine:
    def __init__(self, budget: float = 50.0):
        self.budget = budget

    async def run(
        self,
        trader_trades: dict[str, list[dict]],     # {address: raw trades}
        implied_portfolios: dict[str, float],      # {address: portfolio size}
        display_names: dict[str, Optional[str]],   # {address: name}
        oldest_ts: dict[str, Optional[int]],       # {address: oldest trade ts}
    ) -> FullSimResult:
        """Run full multi-strategy, multi-sizing backtest."""

        # 1. Parse + build positions for all traders
        all_positions: list[MarketPosition] = []
        for addr, raw in trader_trades.items():
            trades = _parse_trades(raw, addr)
            positions = _build_positions(trades, implied_portfolios.get(addr, 5000.0))
            all_positions.extend(positions)

        if not all_positions:
            logger.warning("No positions to simulate")
            return FullSimResult(
                per_trader={}, strategies={}, sizing={}, matrix={}, budget=self.budget
            )

        # 2. Fetch market resolutions
        condition_ids = list({p.condition_id for p in all_positions})
        logger.info("Fetching gamma data for %d markets...", len(condition_ids))
        market_info = await get_market_info(condition_ids)

        # 3. Filter to closed markets only
        closed_positions = [
            p for p in all_positions
            if market_info.get(p.condition_id) and market_info[p.condition_id].closed
        ]
        open_count = len(all_positions) - len(closed_positions)
        logger.info(
            "Positions: %d closed, %d open/unknown (skipping open)",
            len(closed_positions), open_count
        )

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 4. Run all strategy × sizing combinations
        all_bets: list[BetResult] = []
        for pos in closed_positions:
            mi = market_info[pos.condition_id]
            for strategy in _FILTERS:
                for sizing in _SIZERS:
                    bet = _simulate_position(
                        pos, mi, strategy, sizing, self.budget, closed_positions, now_ts
                    )
                    if bet:
                        all_bets.append(bet)

        # 5. Build per-trader results (pure_follow / proportional)
        per_trader: dict[str, TraderResult] = {}
        for addr in trader_trades:
            trader_bets = [
                b for b in all_bets
                if b.trader_address == addr
                and b.strategy == "pure_follow"
                and b.sizing == "proportional"
            ]
            if not trader_bets:
                continue
            total_pnl = sum(b.our_pnl for b in trader_bets)
            won = sum(1 for b in trader_bets if b.won)
            lost = sum(1 for b in trader_bets if not b.won)
            # Days from oldest trade
            ots = oldest_ts.get(addr)
            days = max(1, int((now_ts - ots) / 86400)) if ots else 30
            per_trader[addr] = TraderResult(
                trader_address=addr,
                display_name=display_names.get(addr),
                budget=self.budget,
                our_pnl=round(total_pnl, 2),
                our_pnl_pct=round(total_pnl / self.budget * 100, 1),
                simulated_days=days,
                total_markets=won + lost,
                won_markets=won,
                lost_markets=lost,
                open_markets=0,
            )

        # 6. Build aggregate results by strategy (proportional sizing)
        strategies: dict[str, AggregateResult] = {}
        for strategy in _FILTERS:
            bets = [b for b in all_bets if b.strategy == strategy and b.sizing == "proportional"]
            pnl = sum(b.our_pnl for b in bets)
            won = sum(1 for b in bets if b.won)
            strategies[strategy] = AggregateResult(
                strategy=strategy,
                sizing="proportional",
                budget=self.budget,
                our_pnl=round(pnl, 2),
                our_pnl_pct=round(pnl / self.budget * 100, 1),
                total_bets=len(bets),
                won_bets=won,
                lost_bets=len(bets) - won,
            )

        # 7. Build aggregate results by sizing (pure_follow strategy)
        sizing_results: dict[str, AggregateResult] = {}
        for sizing in _SIZERS:
            bets = [b for b in all_bets if b.strategy == "pure_follow" and b.sizing == sizing]
            pnl = sum(b.our_pnl for b in bets)
            won = sum(1 for b in bets if b.won)
            sizing_results[sizing] = AggregateResult(
                strategy="pure_follow",
                sizing=sizing,
                budget=self.budget,
                our_pnl=round(pnl, 2),
                our_pnl_pct=round(pnl / self.budget * 100, 1),
                total_bets=len(bets),
                won_bets=won,
                lost_bets=len(bets) - won,
            )

        # 8. Full matrix
        matrix: dict[tuple[str, str], AggregateResult] = {}
        for strategy in _FILTERS:
            for sizing in _SIZERS:
                bets = [b for b in all_bets if b.strategy == strategy and b.sizing == sizing]
                pnl = sum(b.our_pnl for b in bets)
                won = sum(1 for b in bets if b.won)
                matrix[(strategy, sizing)] = AggregateResult(
                    strategy=strategy,
                    sizing=sizing,
                    budget=self.budget,
                    our_pnl=round(pnl, 2),
                    our_pnl_pct=round(pnl / self.budget * 100, 1),
                    total_bets=len(bets),
                    won_bets=won,
                    lost_bets=len(bets) - won,
                )

        return FullSimResult(
            per_trader=per_trader,
            strategies=strategies,
            sizing=sizing_results,
            matrix=matrix,
            budget=self.budget,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_full_simulation(
    traders: list,
    data_client,
    budget: float = 50.0,
) -> FullSimResult:
    """
    Fetch all trade history, run full multi-strategy backtest.

    Args:
        traders: List of Trader model objects (need .address, .category_strengths, .display_name).
        data_client: DataApiClient instance.
        budget: Starting budget in USDC.

    Returns:
        FullSimResult with per_trader, strategies, sizing, matrix breakdowns.
    """
    candidates = [
        t for t in traders
        if t.status in ("active", "watching") and (t.total_pnl or 0) > 0
    ]
    if not candidates:
        logger.warning("No qualifying traders for simulation")
        return FullSimResult(per_trader={}, strategies={}, sizing={}, matrix={}, budget=budget)

    # Fetch all trade histories in parallel (small batches to respect rate limits)
    trader_trades: dict[str, list[dict]] = {}
    implied_portfolios: dict[str, float] = {}
    display_names: dict[str, Optional[str]] = {}
    oldest_ts: dict[str, Optional[int]] = {}

    logger.info("Fetching trade history for %d traders...", len(candidates))

    for i in range(0, len(candidates), 5):
        batch = candidates[i : i + 5]
        fetch_tasks = {t.address: data_client.get_all_user_trades(t.address) for t in batch}
        results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)
        for trader, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch trades for %s: %s", trader.address[:10], result)
                continue
            trader_trades[trader.address] = result or []
            extra = trader.category_strengths or {}
            implied_portfolios[trader.address] = float(extra.get("implied_portfolio") or 0)
            display_names[trader.address] = trader.display_name
            ts_list = [int(t["timestamp"]) for t in (result or []) if t.get("timestamp")]
            oldest_ts[trader.address] = min(ts_list) if ts_list else None
        await asyncio.sleep(0.2)

    engine = FullBacktestEngine(budget=budget)
    return await engine.run(trader_trades, implied_portfolios, display_names, oldest_ts)


# ---------------------------------------------------------------------------
# Legacy single-trader entry point (kept for compatibility)
# ---------------------------------------------------------------------------

async def run_simulations(
    traders: list,
    data_client,
    budget: float = 50.0,
) -> dict:
    """Legacy: returns {address: TraderResult}. Use run_full_simulation for new code."""
    full = await run_full_simulation(traders, data_client, budget=budget)
    return full.per_trader
