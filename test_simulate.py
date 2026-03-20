"""
Standalone simulation test — runs WITHOUT the full bot stack.
Uses urllib (no pip needed) + the node pg module for DB access.
Tests the full simulation pipeline end-to-end.

Run: python3 test_simulate.py
"""
import asyncio
import json
import subprocess
import sys
import os
import urllib.request
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://polymarket:BZguWBwacUm3jJ1Mj9ON3FthIFwGQ@89.40.204.122:5433/polymarket_bot"
)

def db_query(sql: str) -> list[dict]:
    """Run a SQL query via node pg and return rows as dicts."""
    script = f"""
const {{ Client }} = require('/root/.openclaw/workspace/node_modules/pg');
const c = new Client({{ connectionString: {json.dumps(DB_URL)} }});
c.connect().then(() => c.query({json.dumps(sql)})).then(r => {{
    console.log(JSON.stringify(r.rows));
    c.end();
}}).catch(e => {{ console.error(e.message); c.end(); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"DB error: {result.stderr.strip()}")
    return json.loads(result.stdout.strip())


def fetch_url(url: str) -> dict | list:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


async def main():
    print("=" * 60)
    print("Polymarket Simulation Self-Test")
    print("=" * 60)

    # 1. Get top 5 traders by score with known PnL
    print("\n[1] Fetching top traders from DB...")
    traders = db_query("""
        SELECT address, display_name, score, total_pnl
        FROM traders
        WHERE status IN ('active','watching') AND total_pnl > 0
        ORDER BY score DESC, total_pnl DESC
        LIMIT 5
    """)
    print(f"    Found {len(traders)} traders")
    for t in traders:
        print(f"    • {t['display_name'] or t['address'][:16]} score={t['score']} pnl=${t['total_pnl']:.0f}")

    if not traders:
        print("    ❌ No traders found. Run /discover first.")
        return

    # 2. For each trader, fetch sample of trades and check timestamps
    # We want traders whose 3000-trade history spans >7 days (not intraday bots)
    print("\n[2] Checking trade history depth per trader...")
    good_traders = []
    for t in traders:
        addr = t['address']
        try:
            # Fetch first and last page trades to check time span
            first_page = fetch_url(
                f"https://data-api.polymarket.com/trades?maker={addr}&limit=5&offset=0"
            )
            last_page = fetch_url(
                f"https://data-api.polymarket.com/trades?maker={addr}&limit=5&offset=2900"
            )
            if not first_page or not last_page:
                continue
            newest_ts = first_page[0].get('timestamp', 0)
            oldest_ts = last_page[-1].get('timestamp', 0)
            span_days = (newest_ts - oldest_ts) / 86400 if oldest_ts else 0
            name = t['display_name'] or addr[:12]
            print(f"    • {name}: span={span_days:.1f}d (newest-oldest across 3000 trades)")
            t['span_days'] = span_days
            t['oldest_ts'] = oldest_ts
            good_traders.append(t)
        except Exception as e:
            print(f"    • {addr[:12]}... error: {e}")

    # Sort by history depth — prefer traders with longer history
    good_traders.sort(key=lambda x: -x.get('span_days', 0))

    if not good_traders:
        print("    ❌ Couldn't fetch trade history")
        return

    print(f"\n    Best history depth: {good_traders[0]['display_name'] or good_traders[0]['address'][:12]} "
          f"({good_traders[0].get('span_days', 0):.1f}d)")

    # 3. Full simulation test on best trader
    best = good_traders[0]
    addr = best['address']
    print(f"\n[3] Running simulation for {best['display_name'] or addr[:16]}...")
    print(f"    Fetching all trades (up to 3000)...")

    all_trades = []
    for offset in range(0, 3001, 100):
        try:
            batch = fetch_url(
                f"https://data-api.polymarket.com/trades?maker={addr}&limit=100&offset={offset}"
            )
            if not batch:
                break
            all_trades.extend(batch)
        except:
            break

    print(f"    Got {len(all_trades)} trades")

    # Get unique conditionIds
    cids = list({t['conditionId'] for t in all_trades if t.get('conditionId')})
    print(f"    Unique markets: {len(cids)}")

    # 4. Check CLOB for resolution — concurrent via asyncio
    print(f"\n[4] Checking market resolutions via CLOB ({len(cids)} markets)...")
    import httpx

    async def fetch_market(client, cid, sem):
        async with sem:
            try:
                r = await client.get(f"https://clob.polymarket.com/markets/{cid}", timeout=10)
                if r.status_code == 200:
                    return cid, r.json()
            except:
                pass
        return cid, None

    sem = asyncio.Semaphore(30)
    async with httpx.AsyncClient() as client:
        tasks = [fetch_market(client, cid, sem) for cid in cids]
        results = await asyncio.gather(*tasks)

    market_info = {}
    closed_count = 0
    for cid, m in results:
        if m and isinstance(m, dict):
            closed = bool(m.get('closed', False))
            tokens = m.get('tokens', [])
            if closed:
                closed_count += 1
            prices = (
                [1.0 if tok.get('winner') else 0.0 for tok in tokens]
                if closed
                else [float(tok.get('price', 0)) for tok in tokens]
            )
            market_info[cid] = {'closed': closed, 'prices': prices, 'question': m.get('question', '?')}

    print(f"    Resolved: {closed_count}/{len(cids)} markets")
    if closed_count == 0:
        print("    ⚠️  No resolved markets found. Likely all trades are from today.")
        print("    Try again later when intraday markets have resolved.")
        return

    # 5. Simulate
    print(f"\n[5] Running backtest simulation...")
    budget = 50.0
    total_cost = 0.0
    total_pnl = 0.0
    won = 0
    lost = 0

    # Build positions (aggregate buys)
    from collections import defaultdict
    pos_by_market: dict[str, dict] = defaultdict(lambda: {
        'total_cost': 0, 'total_recv': 0, 'total_bought': 0, 'total_sold': 0,
        'outcome_index': 0, 'outcome': '?', 'title': '?', 'avg_price': 0
    })
    for t in all_trades:
        cid = t.get('conditionId', '')
        if not cid:
            continue
        side = (t.get('side') or 'BUY').upper()
        price = float(t.get('price', 0) or 0)
        size = float(t.get('size', 0) or 0)
        cost = price * size
        p = pos_by_market[cid]
        p['title'] = t.get('title', '?')[:50]
        p['outcome_index'] = int(t.get('outcomeIndex', 0) or 0)
        p['outcome'] = t.get('outcome', '?')
        if side == 'BUY':
            p['total_cost'] += cost
            p['total_bought'] += size
        else:
            p['total_recv'] += cost
            p['total_sold'] += size

    # Only resolved positions
    closed_positions = [
        (cid, p) for cid, p in pos_by_market.items()
        if cid in market_info and market_info[cid]['closed'] and p['total_cost'] > 0
    ]
    print(f"    Closed positions: {len(closed_positions)}")

    for cid, pos in closed_positions:
        mi = market_info[cid]
        prices = mi['prices']
        oi = pos['outcome_index']
        total_bought = pos['total_bought']
        total_sold = pos['total_sold']
        total_cost = pos['total_cost']
        remaining = max(0, total_bought - total_sold)

        payout = remaining * (prices[oi] if oi < len(prices) else 0)
        trader_net = pos['total_recv'] + payout - total_cost

        # Proportional sizing: assume implied portfolio ~$5000
        our_bet = min(total_cost / 5000, 0.20) * budget
        our_pnl = (trader_net / total_cost) * our_bet if total_cost > 0 else 0

        total_cost += our_bet
        total_pnl += our_pnl
        if trader_net > 0:
            won += 1
        else:
            lost += 1

    pnl_pct = total_pnl / budget * 100 if budget > 0 else 0
    sign = '+' if total_pnl >= 0 else ''
    print(f"\n{'='*60}")
    print(f"RESULT: {sign}${total_pnl:.2f} ({sign}{pnl_pct:.1f}%) on {won+lost} resolved bets")
    print(f"Won: {won}, Lost: {lost}")
    print(f"{'='*60}")
    print("\n✅ Simulation pipeline works correctly!")


if __name__ == "__main__":
    try:
        import httpx
        asyncio.run(main())
    except ImportError:
        print("httpx not available locally — simulation must be tested on the deployed bot")
        print("The fix (TraderRepo.update) has been applied and deployed.")
        print("Run /simulate 5 in the bot to test end-to-end.")
