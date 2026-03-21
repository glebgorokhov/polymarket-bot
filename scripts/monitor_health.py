#!/usr/bin/env python3
"""
Live health monitor — runs every 20 min for 6 hours.
Checks signals, positions, balance, and reports issues.
"""
import asyncio
import asyncpg
import httpx
import sys
import os
from datetime import datetime, timezone, timedelta

DB_CONF = dict(
    host="89.40.204.122", port=5433, database="polymarket_bot",
    user="polymarket", password="BZguWBwacUm3jJ1Mj9ON3FthIFwGQ"
)
BOT_TOKEN = "8678006272:AAEkebzh8AwVYBD2q3XHfN5weFX-hFhXe5g"
ADMIN_ID  = "2174935"
PRIV_KEY  = "0xcd42154024becc5e275c9c9cc651317d873236809bbb160c5aff2b2398b7145e"
SIGNER    = "0x807b6AFB4D652e34baFE2322630ba0E128Eb28D9"
USDC_E    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
RPC       = "https://polygon-bor-rpc.publicnode.com"

def tg(msg: str):
    try:
        httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[tg error] {e}")

def onchain_balance() -> float:
    try:
        data = "0x70a08231" + "000000000000000000000000" + SIGNER[2:].lower()
        r = httpx.post(RPC, json={"jsonrpc":"2.0","method":"eth_call",
                                   "params":[{"to":USDC_E,"data":data},"latest"],"id":1}, timeout=8)
        return int(r.json()["result"], 16) / 1e6
    except:
        return -1.0

async def check(conn, check_num: int, start_time: datetime) -> str:
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=22)  # slight overlap
    elapsed_min = int((now - start_time).total_seconds() / 60)

    lines = [f"📊 <b>Health check #{check_num}</b> (+{elapsed_min}min)"]

    # 1. Mode & budget
    settings = {r['key']: r['value'] for r in await conn.fetch("SELECT key, value FROM settings")}
    mode = settings.get('mode', '?')
    budget = float(settings.get('budget_total', 0))
    mode_icon = "✅" if mode == "auto" else "⚠️"
    lines.append(f"{mode_icon} Mode: <b>{mode}</b>  Budget: <b>${budget:.2f}</b>")

    # 2. On-chain balance
    bal = onchain_balance()
    bal_icon = "✅" if bal >= budget * 0.5 else "⚠️"
    lines.append(f"{bal_icon} USDC.e on-chain: <b>${bal:.2f}</b>")

    # 3. Signals in last 22 min
    sigs = await conn.fetch("""
        SELECT action_taken, skip_reason, COUNT(*) as n
        FROM signals WHERE detected_at >= $1
        GROUP BY action_taken, skip_reason
        ORDER BY n DESC
    """, since)

    total_sigs = sum(r['n'] for r in sigs)
    opened = sum(r['n'] for r in sigs if r['action_taken'] == 'opened')
    skipped = sum(r['n'] for r in sigs if r['action_taken'] == 'skipped')
    manual  = sum(r['n'] for r in sigs if r['action_taken'] == 'manual')
    pending = sum(r['n'] for r in sigs if not r['action_taken'])
    errors  = sum(r['n'] for r in sigs if r['action_taken'] == 'error')

    lines.append(f"\n<b>Signals (last 20min):</b> {total_sigs} total")
    if opened:   lines.append(f"  🟢 Opened: {opened}")
    if skipped:  lines.append(f"  ⏭️ Skipped: {skipped}")
    if manual:   lines.append(f"  👁 Manual: {manual}")
    if pending:  lines.append(f"  ⏳ Pending: {pending}")
    if errors:   lines.append(f"  ❌ Errors: {errors}")

    # Break down skip reasons
    skip_reasons = {}
    for r in sigs:
        if r['action_taken'] == 'skipped' and r['skip_reason']:
            skip_reasons[r['skip_reason']] = skip_reasons.get(r['skip_reason'], 0) + r['n']
    if skip_reasons:
        top = sorted(skip_reasons.items(), key=lambda x: -x[1])[:3]
        for reason, cnt in top:
            lines.append(f"     · {reason}: {cnt}")

    # 4. Open positions
    pos_rows = await conn.fetch("""
        SELECT COUNT(*) as n, is_shadow,
               SUM(size_usd) as total_usd,
               AVG(CASE WHEN current_price > 0 AND entry_price > 0
                        THEN (current_price - entry_price) / entry_price * 100
                        ELSE 0 END) as avg_pnl_pct
        FROM positions WHERE status='open'
        GROUP BY is_shadow
    """)
    real_pos  = next((r for r in pos_rows if not r['is_shadow']), None)
    shadow_pos = next((r for r in pos_rows if r['is_shadow']), None)

    lines.append(f"\n<b>Open positions:</b>")
    if real_pos:
        pnl = real_pos['avg_pnl_pct'] or 0
        pnl_icon = "📈" if pnl >= 0 else "📉"
        lines.append(f"  {pnl_icon} Real: {real_pos['n']} pos, ${real_pos['total_usd']:.2f} deployed, avg {pnl:+.1f}%")
    else:
        lines.append(f"  Real: 0 open")
    # Shadow positions intentionally hidden — they run in background for strategy comparison

    # 5. Recent trades placed (last 22 min)
    recent_trades = await conn.fetch("""
        SELECT p.market_name, p.side, p.entry_price, p.size_usd, p.is_shadow,
               s.trader_id
        FROM positions p
        LEFT JOIN signals s ON p.signal_id = s.id
        WHERE p.opened_at >= $1
        ORDER BY p.opened_at DESC LIMIT 5
    """, since)
    if recent_trades:
        lines.append(f"\n<b>New trades (last 20min):</b>")
        for tr in recent_trades:
            kind = "👻" if tr['is_shadow'] else "💸"
            mkt = (tr['market_name'] or "?")[:40]
            lines.append(f"  {kind} {tr['side']} @ {tr['entry_price']:.3f} ${tr['size_usd']:.2f} — {mkt}")

    # 6. Warnings
    warnings = []
    if mode != "auto":
        warnings.append("⚠️ Mode is not auto!")
    if bal >= 0 and bal < budget * 0.3:
        warnings.append(f"⚠️ Balance low: ${bal:.2f}")
    if total_sigs == 0 and elapsed_min > 30:
        warnings.append("⚠️ No signals in 20min — bot may be stuck")

    if warnings:
        lines.append("\n" + "\n".join(warnings))
    else:
        lines.append("\n✅ All systems nominal")

    return "\n".join(lines)


async def main():
    start_time = datetime.now(timezone.utc)
    check_num = 0
    max_checks = 18  # 18 × 20min = 6 hours

    tg(f"🤖 <b>Monitoring started</b>\nWill check every 20min for 6 hours.\nBot is in <b>auto</b> mode with $48.91 budget.")

    while check_num < max_checks:
        await asyncio.sleep(20 * 60)  # 20 minutes
        check_num += 1

        try:
            conn = await asyncpg.connect(**DB_CONF)
            report = await check(conn, check_num, start_time)
            await conn.close()
            tg(report)
            print(f"[{datetime.now().strftime('%H:%M')}] Check #{check_num} sent")
        except Exception as e:
            tg(f"❌ Health check #{check_num} failed: {e}")
            print(f"Check #{check_num} error: {e}")

    tg("🏁 <b>6-hour monitoring complete.</b>")

if __name__ == "__main__":
    asyncio.run(main())
