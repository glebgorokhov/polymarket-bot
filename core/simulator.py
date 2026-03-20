"""Copytrading backtester.

For each tracked trader, replays their historical trades proportionally scaled
to a given budget ($50 default). Uses gamma API for market resolution.

Sizing model:
- their_allocation_pct = their_total_cost_for_market / their_implied_portfolio
- our_bet = min(their_allocation_pct, max_pct_per_trade) * our_budget
- our_pnl = (trader_net_pnl / trader_total_cost) * our_bet

Outcome determination:
- gamma outcomePrices[outcomeIndex]: 1.0 = won, 0.0 = lost
- Only markets with closed=True are included; open positions skipped
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
_BATCH_SIZE = 20  # conditionIds per gamma request


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MarketResult:
    condition_id: str
    title: str
    closed: bool
    outcome_prices: list[float]  # [price_outcome0, price_outcome1, ...]


@dataclass
class MarketSim:
    condition_id: str
    title: str
    outcome: str        # e.g. "Kings"
    outcome_index: int
    our_cost: float     # USDC we'd have bet
    our_pnl: float      # USDC profit/loss
    won: bool


@dataclass
class SimResult:
    trader_address: str
    budget: float
    our_pnl: float          # total USDC profit/loss
    our_pnl_pct: float      # our_pnl / budget * 100
    simulated_days: int
    total_markets: int      # closed markets only
    won_markets: int
    lost_markets: int
    open_markets: int       # skipped (still open)
    market_details: list[MarketSim] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Gamma API helpers
# ---------------------------------------------------------------------------

async def _fetch_market_batch(condition_ids: list[str]) -> list[dict]:
    """Fetch market info for a batch of conditionIds from gamma API."""
    if not condition_ids:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Gamma accepts repeated conditionIds params
            params = [("conditionIds", cid) for cid in condition_ids]
            resp = await client.get(f"{_GAMMA_BASE}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("gamma batch fetch failed: %s", exc)
        return []


async def get_market_resolutions(condition_ids: list[str]) -> dict[str, MarketResult]:
    """Batch-fetch market resolution data. Returns {conditionId: MarketResult}."""
    results: dict[str, MarketResult] = {}

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
            results[cid] = MarketResult(
                condition_id=cid,
                title=m.get("question") or m.get("title") or "?",
                closed=closed,
                outcome_prices=prices,
            )
        if len(condition_ids) > _BATCH_SIZE:
            await asyncio.sleep(0.15)

    return results


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    def __init__(self, budget: float = 50.0, max_pct_per_trade: float = 0.20):
        self.budget = budget
        self.max_pct_per_trade = max_pct_per_trade

    async def simulate_trader(
        self,
        trader_address: str,
        all_trades: list[dict],
        implied_portfolio: float,
        oldest_trade_ts: Optional[float] = None,
    ) -> Optional[SimResult]:
        """
        Simulate copytrading a trader scaled to our budget.

        Returns SimResult or None if insufficient closed-trade data.
        """
        if not all_trades:
            return None

        # Fallback implied portfolio
        if not implied_portfolio or implied_portfolio <= 0:
            # Estimate from trade sizes: avg_cost * ~20 concurrent positions
            costs = [
                float(t.get("price", 0)) * float(t.get("size", 0))
                for t in all_trades
                if t.get("side", "").upper() == "BUY"
            ]
            avg_cost = (sum(costs) / len(costs)) if costs else 50.0
            implied_portfolio = avg_cost * 20

        # Group by conditionId
        by_market: dict[str, list[dict]] = defaultdict(list)
        for t in all_trades:
            cid = t.get("conditionId", "")
            if cid:
                by_market[cid].append(t)

        if not by_market:
            return None

        # Resolve markets via gamma API
        condition_ids = list(by_market.keys())
        market_info = await get_market_resolutions(condition_ids)

        total_our_pnl = 0.0
        won = lost = open_count = 0
        market_details: list[MarketSim] = []

        for cid, trades in by_market.items():
            mi = market_info.get(cid)

            # Only simulate closed markets
            if not mi or not mi.closed:
                open_count += 1
                continue

            buys = [t for t in trades if t.get("side", "").upper() == "BUY"]
            sells = [t for t in trades if t.get("side", "").upper() == "SELL"]

            if not buys:
                continue

            # --- Trader's economics ---
            total_cost = sum(
                float(t.get("price", 0)) * float(t.get("size", 0)) for t in buys
            )
            if total_cost <= 0:
                continue

            total_received_sells = sum(
                float(t.get("price", 0)) * float(t.get("size", 0)) for t in sells
            )
            total_bought_shares = sum(float(t.get("size", 0)) for t in buys)
            total_sold_shares = sum(float(t.get("size", 0)) for t in sells)
            remaining_shares = max(0.0, total_bought_shares - total_sold_shares)

            # outcomeIndex for resolution payout
            outcome_index = int(buys[0].get("outcomeIndex", 0))
            outcome_name = buys[0].get("outcome", "?")

            # Resolution payout for remaining shares
            if remaining_shares > 0 and mi.outcome_prices and outcome_index < len(mi.outcome_prices):
                payout_per_share = mi.outcome_prices[outcome_index]
                resolution_payout = remaining_shares * payout_per_share
            else:
                resolution_payout = 0.0

            trader_net_pnl = total_received_sells + resolution_payout - total_cost

            # --- Scale to our budget ---
            scale = min(total_cost / implied_portfolio, self.max_pct_per_trade)
            our_cost = scale * self.budget
            our_pnl = (trader_net_pnl / total_cost) * our_cost

            total_our_pnl += our_pnl

            if trader_net_pnl > 0:
                won += 1
            else:
                lost += 1

            market_details.append(
                MarketSim(
                    condition_id=cid,
                    title=mi.title[:60],
                    outcome=outcome_name,
                    outcome_index=outcome_index,
                    our_cost=round(our_cost, 4),
                    our_pnl=round(our_pnl, 4),
                    won=(trader_net_pnl > 0),
                )
            )

        if won + lost == 0:
            return None

        # Days
        if oldest_trade_ts:
            days = max(1, int((datetime.now(timezone.utc).timestamp() - oldest_trade_ts) / 86400))
        else:
            days = 30

        return SimResult(
            trader_address=trader_address,
            budget=self.budget,
            our_pnl=round(total_our_pnl, 2),
            our_pnl_pct=round(total_our_pnl / self.budget * 100, 1),
            simulated_days=days,
            total_markets=won + lost,
            won_markets=won,
            lost_markets=lost,
            open_markets=open_count,
            market_details=market_details,
        )


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_simulations(
    traders: list,
    data_client,
    budget: float = 50.0,
) -> dict[str, SimResult]:
    """Run backtest for a list of Trader model objects. Returns {address: SimResult}."""
    engine = BacktestEngine(budget=budget)
    results: dict[str, SimResult] = {}

    for trader in traders:
        address = trader.address
        try:
            trades = await data_client.get_all_user_trades(address)
            if not trades:
                logger.debug("No trades for %s, skipping sim", address[:10])
                continue

            stats = trader.category_strengths or {}
            implied_portfolio = float(stats.get("implied_portfolio") or 0)

            timestamps = [
                int(t["timestamp"]) for t in trades if t.get("timestamp")
            ]
            oldest_ts = min(timestamps) if timestamps else None

            result = await engine.simulate_trader(
                trader_address=address,
                all_trades=trades,
                implied_portfolio=implied_portfolio,
                oldest_trade_ts=oldest_ts,
            )
            if result:
                results[address] = result
                sign = "+" if result.our_pnl >= 0 else ""
                logger.info(
                    "Sim %-12s PnL %s$%.2f (%s%.1f%%) %dd  %dW/%dL",
                    address[:10],
                    sign, result.our_pnl,
                    sign, result.our_pnl_pct,
                    result.simulated_days,
                    result.won_markets, result.lost_markets,
                )
        except Exception as exc:
            logger.warning("Sim failed for %s: %s", address[:10], exc)

        await asyncio.sleep(0.05)

    return results
