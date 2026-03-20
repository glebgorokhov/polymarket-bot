"""
Copytrading backtester: compound growth, multi-strategy, chart generation.

Key design decisions:
- Compound mode (default): bet sizes grow as balance grows → exponential equity curve
- Positions chronologically sorted so compounding is realistic
- implied_portfolio computed from trader's actual trade history (not stored field)
- Weekly timeline emitted for chart generation (QuickChart.io)

Strategies:
  pure_follow  — copy every resolved trade
  whale        — only when trader bet ≥5% of their implied portfolio
  consensus    — 2+ tracked traders bet same outcome within CONSENSUS_WINDOW_HOURS
  recency      — only trades from last RECENCY_DAYS days

Sizing models:
  proportional — scale bet by trader's allocation % × our balance
  fixed        — flat $N per bet (budget / FIXED_TRADES_EXPECTED)
  conviction   — proportional × (1 + bonus for high-confidence cheap entries)
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

_CLOB_BASE = "https://clob.polymarket.com"
_CONCURRENCY = 30

# Strategy params
WHALE_THRESHOLD = 0.05
CONSENSUS_WINDOW_HOURS = 24
RECENCY_DAYS = 60
FIXED_TRADES_EXPECTED = 25
MAX_BET_PCT = 0.20
CONVICTION_MAX_BONUS = 0.5


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
    condition_id: str
    title: str
    trader_address: str
    side: str
    outcome_index: int
    outcome: str
    price: float
    size: float
    cost: float
    timestamp: int


@dataclass
class MarketPosition:
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
    implied_portfolio: float


@dataclass
class BetResult:
    condition_id: str
    title: str
    trader_address: str
    strategy: str
    sizing: str
    our_cost: float
    our_pnl: float
    won: bool
    timestamp: int = 0


@dataclass
class WeeklyPoint:
    week_label: str   # e.g. "2025-W32"
    balance: float
    bets: int


@dataclass
class AggregateResult:
    strategy: str
    sizing: str
    budget: float
    our_pnl: float
    our_pnl_pct: float
    final_balance: float
    total_bets: int
    won_bets: int
    lost_bets: int
    bets_per_month: float
    weekly_timeline: list[WeeklyPoint]

    @property
    def win_rate(self) -> float:
        if self.total_bets == 0:
            return 0.0
        return self.won_bets / self.total_bets

    @property
    def cagr(self) -> float:
        """Compound Annual Growth Rate from first to last bet."""
        if not self.weekly_timeline or self.final_balance <= 0:
            return 0.0
        weeks = len(self.weekly_timeline)
        if weeks < 2:
            return 0.0
        years = weeks / 52
        return ((self.final_balance / self.budget) ** (1 / years) - 1) * 100


@dataclass
class TraderResult:
    trader_address: str
    display_name: Optional[str]
    budget: float
    final_balance: float
    our_pnl: float
    our_pnl_pct: float
    simulated_days: int
    total_markets: int
    won_markets: int
    lost_markets: int
    bets_per_month: float
    weekly_timeline: list[WeeklyPoint]
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def cagr(self) -> float:
        if self.simulated_days < 7 or self.final_balance <= 0:
            return 0.0
        years = self.simulated_days / 365
        return ((self.final_balance / self.budget) ** (1 / years) - 1) * 100


@dataclass
class FullSimResult:
    per_trader: dict[str, TraderResult]
    strategies: dict[str, AggregateResult]
    sizing: dict[str, AggregateResult]
    matrix: dict[tuple[str, str], AggregateResult]
    budget: float
    compound: bool
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_combo(self) -> Optional[AggregateResult]:
        if not self.matrix:
            return None
        return max(self.matrix.values(), key=lambda r: r.our_pnl)


# ---------------------------------------------------------------------------
# CLOB market resolution
# ---------------------------------------------------------------------------

async def _fetch_clob_market(
    client: httpx.AsyncClient,
    condition_id: str,
    sem: asyncio.Semaphore,
) -> Optional[dict]:
    async with sem:
        try:
            resp = await client.get(
                f"{_CLOB_BASE}/markets/{condition_id}", timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("clob fetch failed %s: %s", condition_id[:10], exc)
    return None


async def get_market_info(condition_ids: list[str]) -> dict[str, MarketInfo]:
    """Fetch resolution data from CLOB for a list of conditionIds (concurrent)."""
    results: dict[str, MarketInfo] = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [_fetch_clob_market(client, cid, sem) for cid in condition_ids]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    for cid, raw in zip(condition_ids, raw_results):
        if not raw or isinstance(raw, Exception) or not isinstance(raw, dict):
            continue
        closed = bool(raw.get("closed", False))
        tokens = raw.get("tokens") or []
        prices: list[float] = []
        for tok in tokens:
            if closed:
                prices.append(1.0 if tok.get("winner") else 0.0)
            else:
                try:
                    prices.append(float(tok.get("price", 0) or 0))
                except (ValueError, TypeError):
                    prices.append(0.0)
        results[cid] = MarketInfo(
            condition_id=cid,
            title=raw.get("question") or raw.get("market_slug") or "?",
            closed=closed,
            outcome_prices=prices,
        )

    closed_count = sum(1 for m in results.values() if m.closed)
    logger.info(
        "CLOB market data: %d/%d resolved", closed_count, len(results)
    )
    return results


# ---------------------------------------------------------------------------
# Trade parsing + position building
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


def _compute_implied_portfolio(trades: list[TradeRecord]) -> float:
    """
    Estimate implied portfolio size from actual buy history.
    Uses total capital deployed / assumed average bet pct (10%).
    Floor: $1000.
    """
    total_buy_cost = sum(t.cost for t in trades if t.side == "BUY")
    if total_buy_cost <= 0:
        return 5000.0
    # Assume average position is ~10% of portfolio
    return max(total_buy_cost / 0.10, 1000.0)


def _build_positions(
    trades: list[TradeRecord],
    implied_portfolio: float,
) -> list[MarketPosition]:
    """Aggregate per-conditionId × outcomeIndex into positions."""
    # Group by (conditionId, outcomeIndex) to handle traders who bet both sides
    key_map: dict[tuple[str, int], list[TradeRecord]] = defaultdict(list)
    for t in trades:
        key_map[(t.condition_id, t.outcome_index)].append(t)

    positions: list[MarketPosition] = []
    for (cid, oi), mkt_trades in key_map.items():
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

        positions.append(MarketPosition(
            condition_id=cid,
            title=buys[0].title,
            trader_address=buys[0].trader_address,
            outcome_index=oi,
            outcome=buys[0].outcome,
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

def _size_proportional(pos: MarketPosition, balance: float) -> float:
    if pos.implied_portfolio <= 0:
        return balance * 0.05
    pct = min(pos.total_cost / pos.implied_portfolio, MAX_BET_PCT)
    return pct * balance


def _size_fixed(pos: MarketPosition, balance: float) -> float:
    return balance / FIXED_TRADES_EXPECTED


def _size_conviction(pos: MarketPosition, balance: float) -> float:
    base = _size_proportional(pos, balance)
    distance = abs(0.5 - pos.avg_price)
    bonus = (distance / 0.5) * CONVICTION_MAX_BONUS
    return min(base * (1 + bonus), MAX_BET_PCT * balance)


_SIZERS = {
    "proportional": _size_proportional,
    "fixed": _size_fixed,
    "conviction": _size_conviction,
}


# ---------------------------------------------------------------------------
# Strategy filters
# ---------------------------------------------------------------------------

def _filter_pure_follow(pos: MarketPosition, all_positions: list[MarketPosition], now_ts: int) -> bool:
    return True


def _filter_whale(pos: MarketPosition, all_positions: list[MarketPosition], now_ts: int) -> bool:
    if pos.implied_portfolio <= 0:
        return False
    return (pos.total_cost / pos.implied_portfolio) >= WHALE_THRESHOLD


def _filter_consensus(pos: MarketPosition, all_positions: list[MarketPosition], now_ts: int) -> bool:
    window = CONSENSUS_WINDOW_HOURS * 3600
    same = [
        p for p in all_positions
        if p.condition_id == pos.condition_id
        and p.outcome_index == pos.outcome_index
        and p.trader_address != pos.trader_address
        and abs(p.first_trade_ts - pos.first_trade_ts) <= window
    ]
    return len(same) >= 1


def _filter_recency(pos: MarketPosition, all_positions: list[MarketPosition], now_ts: int) -> bool:
    cutoff = now_ts - RECENCY_DAYS * 86400
    return pos.first_trade_ts >= cutoff


_FILTERS = {
    "pure_follow": _filter_pure_follow,
    "whale": _filter_whale,
    "consensus": _filter_consensus,
    "recency": _filter_recency,
}


# ---------------------------------------------------------------------------
# Weekly timeline
# ---------------------------------------------------------------------------

def _build_weekly_timeline(
    bets: list[BetResult],
    budget: float,
    compound: bool,
) -> tuple[list[WeeklyPoint], float]:
    """
    Build week-by-week balance from sorted bets.
    Returns (weekly_points, final_balance).
    """
    if not bets:
        return [], budget

    sorted_bets = sorted(bets, key=lambda b: b.timestamp)
    if not sorted_bets[0].timestamp:
        # No timestamps — just accumulate linearly
        balance = budget
        total_pnl = sum(b.our_pnl for b in bets)
        return [
            WeeklyPoint("start", budget, 0),
            WeeklyPoint("end", round(budget + total_pnl, 2), len(bets)),
        ], round(budget + total_pnl, 2)

    first_ts = sorted_bets[0].timestamp
    balance = budget
    weekly_buckets: dict[int, tuple[float, int]] = {}  # week_num → (balance_after, bet_count)

    for bet in sorted_bets:
        week_num = (bet.timestamp - first_ts) // (7 * 86400)
        if compound:
            sizer = _SIZERS.get(bet.sizing, _SIZERS["proportional"])
            # Recalculate bet size with current balance
            # We stored our_cost as fraction of original budget; scale to current balance
            if budget > 0:
                pct = bet.our_cost / budget
                actual_bet = min(pct, MAX_BET_PCT) * balance
            else:
                actual_bet = bet.our_cost
            # Scale pnl proportionally
            if bet.our_cost > 0:
                pnl = bet.our_pnl * (actual_bet / bet.our_cost)
            else:
                pnl = bet.our_pnl
        else:
            pnl = bet.our_pnl

        balance += pnl
        balance = max(balance, 0.01)  # can't go below $0.01

        prev_balance, prev_count = weekly_buckets.get(week_num, (balance, 0))
        weekly_buckets[week_num] = (balance, prev_count + 1)

    if not weekly_buckets:
        return [], budget

    max_week = max(weekly_buckets.keys())
    points: list[WeeklyPoint] = [WeeklyPoint("W0", round(budget, 2), 0)]
    running_balance = budget
    running_bets = 0
    for w in range(max_week + 1):
        if w in weekly_buckets:
            running_balance, week_bets = weekly_buckets[w]
            running_bets += week_bets
        points.append(WeeklyPoint(f"W{w+1}", round(running_balance, 2), running_bets))

    return points, round(balance, 2)


# ---------------------------------------------------------------------------
# Full simulation engine
# ---------------------------------------------------------------------------

class FullBacktestEngine:
    def __init__(self, budget: float = 50.0, compound: bool = True):
        self.budget = budget
        self.compound = compound

    async def run(
        self,
        trader_trades: dict[str, list[dict]],
        implied_portfolios: dict[str, float],
        display_names: dict[str, Optional[str]],
        oldest_ts: dict[str, Optional[int]],
    ) -> FullSimResult:

        # 1. Parse + build positions
        all_positions: list[MarketPosition] = []
        for addr, raw in trader_trades.items():
            trades = _parse_trades(raw, addr)
            ip = implied_portfolios.get(addr) or _compute_implied_portfolio(trades)
            positions = _build_positions(trades, ip)
            all_positions.extend(positions)

        if not all_positions:
            logger.warning("No positions to simulate")
            return FullSimResult(
                per_trader={}, strategies={}, sizing={}, matrix={},
                budget=self.budget, compound=self.compound
            )

        # 2. Fetch CLOB resolution
        condition_ids = list({p.condition_id for p in all_positions})
        logger.info("Fetching CLOB data for %d markets...", len(condition_ids))
        market_info = await get_market_info(condition_ids)

        # 3. Filter to closed markets only
        closed_positions = [
            p for p in all_positions
            if market_info.get(p.condition_id) and market_info[p.condition_id].closed
        ]
        logger.info(
            "Positions: %d closed, %d open (skipping open)",
            len(closed_positions), len(all_positions) - len(closed_positions)
        )

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 4. Simulate all strategy × sizing combos
        all_bets: list[BetResult] = []
        for pos in closed_positions:
            mi = market_info[pos.condition_id]
            if not mi.outcome_prices or pos.outcome_index >= len(mi.outcome_prices):
                continue

            remaining = max(0.0, pos.remaining_shares)
            if remaining <= 0 and pos.total_received_sells <= 0:
                continue  # no exposure

            payout = remaining * mi.outcome_prices[pos.outcome_index]
            trader_net = pos.total_received_sells + payout - pos.total_cost
            if pos.total_cost <= 0:
                continue

            for strategy in _FILTERS:
                if not _FILTERS[strategy](pos, closed_positions, now_ts):
                    continue
                for sizing, sizer in _SIZERS.items():
                    our_cost = sizer(pos, self.budget)  # base cost at original budget
                    if our_cost <= 0:
                        continue
                    our_pnl = (trader_net / pos.total_cost) * our_cost

                    all_bets.append(BetResult(
                        condition_id=pos.condition_id,
                        title=pos.title[:50],
                        trader_address=pos.trader_address,
                        strategy=strategy,
                        sizing=sizing,
                        our_cost=our_cost,
                        our_pnl=our_pnl,
                        won=(trader_net > 0),
                        timestamp=pos.first_trade_ts,
                    ))

        # 5. Per-trader results (pure_follow / proportional)
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
            timeline, final_balance = _build_weekly_timeline(
                trader_bets, self.budget, self.compound
            )
            total_pnl = final_balance - self.budget
            won = sum(1 for b in trader_bets if b.won)
            lost = sum(1 for b in trader_bets if not b.won)
            ots = oldest_ts.get(addr)
            days = max(1, int((now_ts - ots) / 86400)) if ots else 30
            bets_per_month = (won + lost) / max(days / 30, 0.1)

            per_trader[addr] = TraderResult(
                trader_address=addr,
                display_name=display_names.get(addr),
                budget=self.budget,
                final_balance=round(final_balance, 2),
                our_pnl=round(total_pnl, 2),
                our_pnl_pct=round(total_pnl / self.budget * 100, 1),
                simulated_days=days,
                total_markets=won + lost,
                won_markets=won,
                lost_markets=lost,
                bets_per_month=round(bets_per_month, 1),
                weekly_timeline=timeline,
            )

        # 6. Strategy comparison (proportional sizing)
        strategies: dict[str, AggregateResult] = {}
        for strategy in _FILTERS:
            bets = [b for b in all_bets if b.strategy == strategy and b.sizing == "proportional"]
            timeline, final_balance = _build_weekly_timeline(bets, self.budget, self.compound)
            pnl = final_balance - self.budget
            won = sum(1 for b in bets if b.won)
            total_ts = [b.timestamp for b in bets if b.timestamp]
            span_days = (max(total_ts) - min(total_ts)) / 86400 if len(total_ts) > 1 else 30
            bets_pm = len(bets) / max(span_days / 30, 0.1)
            strategies[strategy] = AggregateResult(
                strategy=strategy, sizing="proportional",
                budget=self.budget, our_pnl=round(pnl, 2),
                our_pnl_pct=round(pnl / self.budget * 100, 1),
                final_balance=round(final_balance, 2),
                total_bets=len(bets), won_bets=won, lost_bets=len(bets) - won,
                bets_per_month=round(bets_pm, 1),
                weekly_timeline=timeline,
            )

        # 7. Sizing comparison (pure_follow strategy)
        sizing_results: dict[str, AggregateResult] = {}
        for sizing in _SIZERS:
            bets = [b for b in all_bets if b.strategy == "pure_follow" and b.sizing == sizing]
            timeline, final_balance = _build_weekly_timeline(bets, self.budget, self.compound)
            pnl = final_balance - self.budget
            won = sum(1 for b in bets if b.won)
            total_ts = [b.timestamp for b in bets if b.timestamp]
            span_days = (max(total_ts) - min(total_ts)) / 86400 if len(total_ts) > 1 else 30
            bets_pm = len(bets) / max(span_days / 30, 0.1)
            sizing_results[sizing] = AggregateResult(
                strategy="pure_follow", sizing=sizing,
                budget=self.budget, our_pnl=round(pnl, 2),
                our_pnl_pct=round(pnl / self.budget * 100, 1),
                final_balance=round(final_balance, 2),
                total_bets=len(bets), won_bets=won, lost_bets=len(bets) - won,
                bets_per_month=round(bets_pm, 1),
                weekly_timeline=timeline,
            )

        # 8. Full matrix
        matrix: dict[tuple[str, str], AggregateResult] = {}
        for strategy in _FILTERS:
            for sizing in _SIZERS:
                bets = [b for b in all_bets if b.strategy == strategy and b.sizing == sizing]
                timeline, final_balance = _build_weekly_timeline(bets, self.budget, self.compound)
                pnl = final_balance - self.budget
                won = sum(1 for b in bets if b.won)
                total_ts = [b.timestamp for b in bets if b.timestamp]
                span_days = (max(total_ts) - min(total_ts)) / 86400 if len(total_ts) > 1 else 30
                bets_pm = len(bets) / max(span_days / 30, 0.1)
                matrix[(strategy, sizing)] = AggregateResult(
                    strategy=strategy, sizing=sizing,
                    budget=self.budget, our_pnl=round(pnl, 2),
                    our_pnl_pct=round(pnl / self.budget * 100, 1),
                    final_balance=round(final_balance, 2),
                    total_bets=len(bets), won_bets=won, lost_bets=len(bets) - won,
                    bets_per_month=round(bets_pm, 1),
                    weekly_timeline=timeline,
                )

        return FullSimResult(
            per_trader=per_trader,
            strategies=strategies,
            sizing=sizing_results,
            matrix=matrix,
            budget=self.budget,
            compound=self.compound,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_full_simulation(
    traders: list,
    data_client,
    budget: float = 50.0,
    compound: bool = True,
) -> FullSimResult:
    """
    Fetch all trade history for traders, run full multi-strategy backtest.

    Args:
        traders: List of Trader model objects.
        data_client: DataApiClient instance.
        budget: Starting budget in USDC.
        compound: If True, bet sizes grow with running balance (reinvestment).

    Returns:
        FullSimResult with per_trader, strategies, sizing, matrix.
    """
    if not traders:
        logger.warning("No traders passed to simulation")
        return FullSimResult(per_trader={}, strategies={}, sizing={}, matrix={},
                             budget=budget, compound=compound)

    trader_trades: dict[str, list[dict]] = {}
    implied_portfolios: dict[str, float] = {}
    display_names: dict[str, Optional[str]] = {}
    oldest_ts: dict[str, Optional[int]] = {}

    logger.info("Fetching trade history for %d traders...", len(traders))

    for i in range(0, len(traders), 5):
        batch = traders[i: i + 5]
        tasks = {t.address: data_client.get_all_user_trades(t.address) for t in batch}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for trader, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.warning("Trade fetch failed %s: %s", trader.address[:10], result)
                continue
            raw = result or []
            trader_trades[trader.address] = raw
            display_names[trader.address] = trader.display_name
            ts_list = [int(t["timestamp"]) for t in raw if t.get("timestamp")]
            oldest_ts[trader.address] = min(ts_list) if ts_list else None
            # Compute implied portfolio from actual trade data
            from core.simulator import _parse_trades, _compute_implied_portfolio
            trades_parsed = _parse_trades(raw, trader.address)
            implied_portfolios[trader.address] = _compute_implied_portfolio(trades_parsed)
        await asyncio.sleep(0.2)

    engine = FullBacktestEngine(budget=budget, compound=compound)
    return await engine.run(trader_trades, implied_portfolios, display_names, oldest_ts)


async def run_simulations(traders: list, data_client, budget: float = 50.0) -> dict:
    """Legacy alias."""
    full = await run_full_simulation(traders, data_client, budget=budget)
    return full.per_trader
