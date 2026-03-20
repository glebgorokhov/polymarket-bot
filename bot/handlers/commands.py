"""
All Telegram slash command handlers.
Every handler silently ignores requests from non-admin users.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import (
    mode_keyboard,
    positions_refresh_keyboard,
    status_keyboard,
    traders_keyboard,
)
from config import get_settings
from db.repos.positions import PositionRepo
from db.repos.settings import SettingsRepo
from db.repos.signals import SignalRepo
from db.repos.strategies import StrategyRepo
from db.repos.traders import TraderRepo
from db.session import get_session

logger = logging.getLogger(__name__)

_TRADERS_PER_PAGE = 5
STRATEGY_LETTERS = {"pure_follow": "A", "consensus": "B", "whale": "C", "category_expert": "D", "smart_exit": "E"}


def _is_admin(update: Update) -> bool:
    """Check if the message sender is the configured admin."""
    cfg = get_settings()
    user = update.effective_user
    return user is not None and user.id == cfg.telegram_admin_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /start command. Shows a welcome message and quick status.
    """
    if not _is_admin(update):
        return
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        mode = await settings_repo.get("mode", "manual")
        active_strategy_slug = await settings_repo.get("active_strategy_slug", "consensus")
        position_repo = PositionRepo(session)
        open_positions = await position_repo.get_open()

    text = (
        f"👋 <b>Polymarket Copytrader</b>\n\n"
        f"🔄 Mode: <b>{mode.upper()}</b>\n"
        f"🎯 Strategy: <b>{active_strategy_slug}</b>\n"
        f"📂 Open positions: <b>{len(open_positions)}</b>\n\n"
        f"Use /help to see all commands."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /status command. Shows balance, positions, today P&L, mode, strategy.
    """
    if not _is_admin(update):
        return
    text = await _build_status_text()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=status_keyboard())


async def _build_status_text() -> str:
    """Build the status message string."""
    cfg = get_settings()

    # Get balance from CLOB — requires py-clob-client auth
    balance = None
    balance_source = "live"
    try:
        from api.clob import ClobApiClient
        clob = ClobApiClient(
            relayer_api_key=cfg.relayer_api_key,
            relayer_api_address=cfg.relayer_api_address,
            signer_address=cfg.signer_address,
        )
        raw_balance = await clob.get_balance()
        balance = float(raw_balance or 0)
    except Exception as exc:
        logger.warning("CLOB balance fetch failed: %s", exc)
        balance = None
        balance_source = "unavailable"

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        mode = await settings_repo.get("mode", "manual")
        active_slug = await settings_repo.get("active_strategy_slug", "consensus")
        position_repo = PositionRepo(session)
        real_positions = list(await position_repo.get_open(is_shadow=False))
        shadow_positions = list(await position_repo.get_open(is_shadow=True))
        deployed = sum(p.size_usd for p in real_positions)

        # Today's P&L (real positions only)
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        closed_today = list(await position_repo.get_closed_in_period(today_start, datetime.now(timezone.utc)))
        today_pnl = sum(p.pnl or 0 for p in closed_today if not p.is_shadow)

    shadow_note = f" (+{len(shadow_positions)} shadow)" if shadow_positions else ""

    if balance is not None:
        bal_str = f"${balance:.2f} (live)"
    else:
        bal_str = "⚠️ unavailable (CLOB auth failed)"

    return (
        f"📊 <b>Status</b>\n\n"
        f"💰 Balance: {bal_str}\n"
        f"📦 Deployed: ${deployed:.2f}\n"
        f"📈 Today P&L: {'+' if today_pnl >= 0 else ''}${today_pnl:.2f}\n"
        f"🔄 Mode: <b>{mode.upper()}</b>\n"
        f"🎯 Primary strategy: <b>{active_slug}</b>\n"
        f"📂 Open positions: <b>{len(real_positions)}</b>{shadow_note}"
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /mode <auto|manual|paper>. Switches trading mode.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        async with get_session() as session:
            settings_repo = SettingsRepo(session)
            current = await settings_repo.get("mode", "manual")
        await update.message.reply_text(
            f"Current mode: <b>{current.upper()}</b>\nUsage: /mode <auto|manual|paper>",
            parse_mode="HTML",
            reply_markup=mode_keyboard(current),
        )
        return

    new_mode = args[0].lower()
    if new_mode not in ("auto", "manual", "paper"):
        await update.message.reply_text("❌ Invalid mode. Use: auto, manual, paper")
        return

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("mode", new_mode)

    mode_icons = {"auto": "🟢", "manual": "🟡", "paper": "📝"}
    await update.message.reply_text(
        f"{mode_icons[new_mode]} Mode switched to <b>{new_mode.upper()}</b>",
        parse_mode="HTML",
    )


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /budget <amount>. Sets total budget in USD.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        async with get_session() as session:
            settings_repo = SettingsRepo(session)
            current = await settings_repo.get("budget_total", "50.0")
        await update.message.reply_text(f"Current budget: ${current}\nUsage: /budget <amount>")
        return
    try:
        amount = float(args[0])
        if amount <= 0:
            raise ValueError("Must be positive")
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Use a positive number.")
        return

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("budget_total", str(amount))
    await update.message.reply_text(f"💰 Budget set to ${amount:.2f}")


async def cmd_pertrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /pertrade <pct>. Sets the per-trade budget percentage (e.g., 5 for 5%).
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        async with get_session() as session:
            settings_repo = SettingsRepo(session)
            current = await settings_repo.get("budget_per_trade_pct", "5.0")
        await update.message.reply_text(f"Current per-trade: {current}%\nUsage: /pertrade <pct>")
        return
    try:
        pct = float(args[0])
        if not 0 < pct <= 100:
            raise ValueError("Must be 0-100")
    except ValueError:
        await update.message.reply_text("❌ Invalid percentage. Use a number between 0 and 100.")
        return

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("budget_per_trade_pct", str(pct))
    await update.message.reply_text(f"📊 Per-trade set to {pct:.1f}%")


async def cmd_maxtrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /maxtrade <usd>. Sets the maximum single-trade USD cap.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        async with get_session() as session:
            settings_repo = SettingsRepo(session)
            current = await settings_repo.get("max_trade_usd", "20.0")
        await update.message.reply_text(f"Current max trade: ${current}\nUsage: /maxtrade <usd>")
        return
    try:
        amount = float(args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("max_trade_usd", str(amount))
    await update.message.reply_text(f"🔒 Max trade size set to ${amount:.2f}")


def _format_trader_card(trader) -> str:
    """Format trader card using ground-truth leaderboard metrics."""
    name = trader.display_name or trader.address[:14] + "…"
    profile_url = f"https://polymarket.com/profile/{trader.address}"
    status_icon = {"active": "🟢", "watching": "🟡", "inactive": "🔴"}.get(trader.status, "⚪")

    rank_str = f"  🏅 #{trader.leaderboard_rank}" if trader.leaderboard_rank and trader.leaderboard_rank < 500 else ""
    pin_str = "  📌" if getattr(trader, "is_pinned", False) else ""
    pnl_str = f"+${trader.total_pnl:,.0f}" if (trader.total_pnl or 0) >= 0 else f"-${abs(trader.total_pnl or 0):,.0f}"

    # Capital efficiency: PnL / Volume
    efficiency = None
    extra = trader.category_strengths or {}  # reused as generic extra stats
    if trader.total_pnl and extra.get("avg_bet_pct"):
        # Stored in extra_stats by new scoring
        avg_bet_pct_str = f"~{extra['avg_bet_pct']:.1f}% per bet"
    elif trader.avg_profit_per_trade:
        avg_bet_pct_str = f"${trader.avg_profit_per_trade:+.0f}/position"
    else:
        avg_bet_pct_str = "—"

    # Active duration
    active_days = extra.get("active_days")
    if active_days:
        if active_days >= 365:
            duration_str = f"{active_days // 30}mo"
        elif active_days >= 30:
            duration_str = f"{active_days // 7}wk"
        else:
            duration_str = f"{active_days}d"
    else:
        duration_str = "—"

    # Last active
    if trader.last_active_at:
        from datetime import timezone as tz
        last_dt = trader.last_active_at
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=tz.utc)
        days_ago = (datetime.now(tz.utc) - last_dt).days
        last_active = "today" if days_ago == 0 else f"{days_ago}d ago"
    else:
        last_active = "—"

    trades_per_week = f"{trader.avg_trades_per_week:.0f}/wk" if trader.avg_trades_per_week else "—"

    return (
        f"{status_icon} <b><a href=\"{profile_url}\">{name}</a></b>{rank_str}{pin_str}\n"
        f"💰 {pnl_str}  ·  Score: <b>{trader.score:.3f}</b>\n"
        f"📊 {trader.trade_count:,} positions · {trades_per_week} · {duration_str} active\n"
        f"⚡ {avg_bet_pct_str}  ·  last active {last_active}"
    )


async def cmd_traders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /traders. Paginated list of tracked traders with score, top category, 30d PnL.
    """
    if not _is_admin(update):
        return
    await _send_traders_page(update, 1)


async def _send_traders_page(update: Update, page: int) -> None:
    """Send a page of tracked traders (active first, then watching)."""
    async with get_session() as session:
        trader_repo = TraderRepo(session)
        traders = list(await trader_repo.get_all())

    if not traders:
        await update.message.reply_text("No traders tracked yet. Discovery may still be running.")
        return

    active = sorted([t for t in traders if t.status == "active"], key=lambda t: -(t.score or 0))
    watching = sorted([t for t in traders if t.status == "watching"], key=lambda t: -(t.score or 0))
    inactive = sorted([t for t in traders if t.status == "inactive"], key=lambda t: -(t.score or 0))

    # Active first, then watching, then inactive — each group sorted by score+win_rate
    all_sorted = active + watching + inactive
    total_pages = max(1, (len(all_sorted) + _TRADERS_PER_PAGE - 1) // _TRADERS_PER_PAGE)
    start = (page - 1) * _TRADERS_PER_PAGE
    page_traders = all_sorted[start : start + _TRADERS_PER_PAGE]

    lines = [
        f"👥 <b>Tracked Traders</b> — page {page}/{total_pages}\n"
        f"🟢 {len(active)} monitored  🟡 {len(watching)} watching  🔴 {len(inactive)} inactive\n"
    ]
    for trader in page_traders:
        lines.append(_format_trader_card(trader))
        lines.append("")  # spacing between cards

    keyboard = traders_keyboard(page, total_pages)
    text = "\n".join(lines)
    if hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /positions. Shows all open positions with current value and unrealized P&L.
    """
    if not _is_admin(update):
        return
    text = await _build_positions_text()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=positions_refresh_keyboard())


async def _build_positions_text() -> str:
    """Build positions summary string (real positions only, shadow count shown separately)."""
    async with get_session() as session:
        position_repo = PositionRepo(session)
        real_positions = list(await position_repo.get_open(is_shadow=False))
        shadow_positions = list(await position_repo.get_open(is_shadow=True))

    if not real_positions and not shadow_positions:
        return "📂 No open positions."

    lines = [f"📂 <b>Open Positions ({len(real_positions)} real)</b>\n"]
    for pos in real_positions:
        current = pos.current_price or pos.entry_price
        entry_cost = pos.entry_cost or pos.size_usd
        current_value = (pos.shares or 0) * current
        pnl = current_value - entry_cost
        pnl_pct = (pnl / entry_cost * 100) if entry_cost > 0 else 0
        sign = "+" if pnl >= 0 else ""
        icon = "🟢" if pnl >= 0 else "🔴"
        market_short = pos.market_name[:35] + "..." if len(pos.market_name) > 35 else pos.market_name
        lines.append(
            f"{icon} <b>{market_short}</b>\n"
            f"   {pos.side} @ {pos.entry_price:.4f} | Now: ${current_value:.2f} ({sign}{pnl_pct:.1f}%)"
        )
    if shadow_positions:
        lines.append(f"\n👁️ <i>{len(shadow_positions)} shadow simulation position(s) running in background</i>")
    return "\n".join(lines)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /history [n]. Shows last N closed positions (default 10) with P&L.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    n = 10
    if args:
        try:
            n = max(1, min(50, int(args[0])))
        except ValueError:
            pass

    async with get_session() as session:
        position_repo = PositionRepo(session)
        closed = list(await position_repo.get_closed(limit=n, is_shadow=False))

    if not closed:
        await update.message.reply_text("No closed positions found.")
        return

    lines = [f"📜 <b>Last {len(closed)} Closed Positions</b>\n"]
    for pos in closed:
        pnl = pos.pnl or 0
        pnl_pct = pos.pnl_pct or 0
        sign = "+" if pnl >= 0 else ""
        icon = "✅" if pnl >= 0 else "❌"
        market_short = pos.market_name[:35] + "..." if len(pos.market_name) > 35 else pos.market_name
        closed_str = pos.closed_at.strftime("%m/%d %H:%M") if pos.closed_at else "N/A"
        lines.append(
            f"{icon} <b>{market_short}</b>\n"
            f"   {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%) | {closed_str}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /strategy list and /strategy use <slug>.
    """
    if not _is_admin(update):
        return
    args = context.args or []

    if not args or args[0].lower() == "list":
        await _cmd_strategy_list(update)
        return

    if args[0].lower() == "use" and len(args) >= 2:
        await _cmd_strategy_use(update, args[1].lower())
        return

    await update.message.reply_text(
        "Usage:\n/strategy list\n/strategy use <slug>"
    )


async def _cmd_strategy_list(update: Update) -> None:
    """List all strategies with 7d performance, marking primary and shadow roles."""
    async with get_session() as session:
        strategy_repo = StrategyRepo(session)
        settings_repo = SettingsRepo(session)
        strategies = list(await strategy_repo.get_all())
        primary_slug = await settings_repo.get("active_strategy_slug", "consensus")
        lines = ["🎯 <b>Strategies</b> (🎯 = primary for real trades, 👁️ = shadow simulation)\n"]
        for strat in strategies:
            letter = STRATEGY_LETTERS.get(strat.slug, "?")
            pnl_7d = await strategy_repo.get_7d_pnl(strat.id)
            sign = "+" if pnl_7d >= 0 else ""
            if strat.slug == primary_slug and strat.is_active:
                role_tag = " 🎯 [primary]"
            elif strat.is_active:
                role_tag = " 👁️ [shadow]"
            else:
                role_tag = " ⭕ [disabled]"
            lines.append(
                f"<b>{letter}. {strat.name}</b>{role_tag}\n"
                f"   Slug: <code>{strat.slug}</code> | 7d P&L: {sign}${pnl_7d:.2f}\n"
                f"   {strat.description}"
            )

    await update.message.reply_text("\n\n".join(lines[0:1]) + "\n" + "\n\n".join(lines[1:]), parse_mode="HTML")


async def _cmd_strategy_use(update: Update, slug: str) -> None:
    """Set the primary strategy (the one that places real/paper trades)."""
    async with get_session() as session:
        strategy_repo = StrategyRepo(session)
        settings_repo = SettingsRepo(session)
        found = await strategy_repo.get_by_slug(slug)
        if not found:
            await update.message.reply_text(f"❌ Strategy '{slug}' not found.")
            return
        await settings_repo.set("active_strategy_slug", slug)

    await update.message.reply_text(
        f"✅ Primary strategy set to <b>{slug}</b>\n"
        f"🎯 Real/paper trades will use this strategy.\n"
        f"👁️ All other active strategies continue as shadow simulations.",
        parse_mode="HTML",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /report. Generates and sends the full 6-hour style report.
    """
    if not _is_admin(update):
        return
    await update.message.reply_text("⏳ Generating report...")
    from scheduler import generate_and_send_report
    await generate_and_send_report()


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /pause. Sets mode to manual (pauses auto-trading).
    """
    if not _is_admin(update):
        return
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("mode", "manual")
    await update.message.reply_text("⏸️ Bot paused. Mode set to <b>MANUAL</b>.", parse_mode="HTML")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /resume. Sets mode back to auto.
    """
    if not _is_admin(update):
        return
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("mode", "auto")
    await update.message.reply_text("▶️ Bot resumed. Mode set to <b>AUTO</b>.", parse_mode="HTML")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /settings. Displays all current settings.
    """
    if not _is_admin(update):
        return
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        all_settings = await settings_repo.as_dict()

    if not all_settings:
        await update.message.reply_text("No settings configured.")
        return

    lines = ["⚙️ <b>Settings</b>\n"]
    for key, value in sorted(all_settings.items()):
        lines.append(f"  <b>{key}</b>: <code>{value}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /feed. Shows recent trades from tracked traders grouped by market.
    Highlights markets where multiple traders bet, and opposite bets.
    """
    if not _is_admin(update):
        return

    await update.message.reply_text("🔄 Fetching recent activity from tracked traders...")

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        active_traders = list(await trader_repo.get_active())

    # Limit to top 20 by score to avoid too many API calls
    top_traders = sorted(active_traders, key=lambda t: t.score, reverse=True)[:20]

    if not top_traders:
        await update.message.reply_text("No active traders yet. Run /discover first.")
        return

    from api.data_api import DataApiClient

    # Fetch last 5 trades for each trader concurrently
    market_activity: dict[str, list[dict]] = {}  # condition_id → list of trade+trader info

    async def fetch_trader_trades(trader) -> list[dict]:
        try:
            async with DataApiClient() as client:
                trades = await client.get_trades(user=trader.address, limit=5)
            result = []
            for t in trades:
                result.append({
                    "trader_name": trader.display_name or trader.address[:12] + "…",
                    "trader_address": trader.address,
                    "trader_score": trader.score,
                    "market": t.get("market") or t.get("conditionId") or t.get("condition_id", ""),
                    "market_slug": t.get("market_slug") or t.get("marketSlug", ""),
                    "outcome": t.get("outcome") or t.get("title", ""),
                    "side": t.get("side", "").upper(),
                    "price": float(t.get("price", 0) or 0),
                    "size": float(t.get("size", 0) or 0),
                    "usd_value": float(t.get("size", 0) or 0) * float(t.get("price", 0) or 0),
                    "timestamp": t.get("timestamp", ""),
                })
            return result
        except Exception:
            return []

    import asyncio
    all_results = await asyncio.gather(*[fetch_trader_trades(t) for t in top_traders])

    # Group by market
    for trader_trades in all_results:
        for trade in trader_trades:
            mid = trade["market"]
            if not mid:
                continue
            if mid not in market_activity:
                market_activity[mid] = []
            market_activity[mid].append(trade)

    # Sort markets by number of participating traders (most interesting first)
    sorted_markets = sorted(market_activity.items(), key=lambda x: len(x[1]), reverse=True)

    if not sorted_markets:
        await update.message.reply_text("No recent trade activity found.")
        return

    lines = [f"📰 <b>Recent Market Activity</b> — top {len(top_traders)} traders\n"]
    shown = 0

    for condition_id, trades in sorted_markets[:15]:
        buyers = [t for t in trades if t["side"] == "BUY"]
        sellers = [t for t in trades if t["side"] == "SELL"]

        # Only show markets with actual activity worth noting
        unique_traders = len({t["trader_address"] for t in trades})

        # Determine market name from outcome or slug
        market_name = trades[0].get("market_slug") or trades[0].get("outcome") or condition_id[:20]

        # Conflict indicator
        if buyers and sellers:
            conflict = " ⚡ <i>SPLIT</i>"
        elif len(set(t["trader_address"] for t in trades)) > 1:
            conflict = " 🤝 <i>consensus</i>"
        else:
            conflict = ""

        lines.append(f"🏪 <b>{market_name}</b>{conflict}")

        for t in sorted(trades, key=lambda x: x["usd_value"], reverse=True)[:5]:
            side_icon = "🔵" if t["side"] == "BUY" else "🔴"
            lines.append(
                f"  {side_icon} {t['trader_name']} — {t['side']} @ {t['price']:.2f} · ${t['usd_value']:.0f}"
            )
        lines.append("")
        shown += 1

    if not shown:
        lines.append("No notable activity in the last 5 trades per trader.")

    # Telegram message limit is 4096 chars — truncate if needed
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /signals. Shows last 20 signals across all tracked traders.
    """
    if not _is_admin(update):
        return

    async with get_session() as session:
        signal_repo = SignalRepo(session)
        signals = list(await signal_repo.get_latest(limit=20))

    if not signals:
        await update.message.reply_text("📭 No signals yet. Run /discover to find traders.")
        return

    lines = [f"📡 <b>Last {len(signals)} signals</b>\n"]
    for sig in signals:
        ts = sig.detected_at.strftime("%m/%d %H:%M") if sig.detected_at else "—"
        action_icon = {"copied": "✅", "paper": "📄", "skipped": "⏭️", "manual": "⚠️"}.get(
            sig.action_taken or "", "❓"
        )
        trader = sig.trader if hasattr(sig, "trader") and sig.trader else None
        trader_name = (
            (trader.display_name or trader.address[:10] + "…") if trader else "unknown"
        )
        strategies_hit = sig.strategies_triggered or []
        strat_str = " · ".join(strategies_hit) if strategies_hit else "—"
        market_short = (sig.market_name or sig.market_condition_id or "")[:35]
        side_icon = "🔵" if sig.side == "BUY" else "🔴"
        lines.append(
            f"{action_icon} {side_icon} <b>{market_short}</b>\n"
            f"   {trader_name} · {sig.side} @ {sig.price:.4f} · ${sig.size_usd:.2f}\n"
            f"   Strategies: {strat_str} · {ts}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /track <address_or_username>. Manually pin a trader for active monitoring.
    Bypasses scoring gates — pinned traders are never auto-dropped.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /track <address>\nExample: /track 0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
        )
        return

    target = args[0].strip()
    address = target if target.startswith("0x") else None

    # If not an address, look up by username via leaderboard
    if not address:
        try:
            from api.data_api import DataApiClient
            async with DataApiClient() as client:
                lb = await client.get_leaderboard(limit=200, category="OVERALL")
            for e in lb:
                name = (e.get("userName") or "").lower()
                if name == target.lower():
                    address = e.get("proxyWallet", "")
                    break
        except Exception as exc:
            logger.error("Leaderboard lookup failed: %s", exc)

    if not address:
        await update.message.reply_text(f"❌ Couldn't find trader: {target}\nTip: use the full 0x address for reliability.")
        return

    # Fetch their profile from leaderboard for display name and PnL
    lb_entry: dict = {}
    try:
        from api.data_api import DataApiClient
        async with DataApiClient() as client:
            lb = await client.get_leaderboard(limit=200, category="OVERALL")
        for e in lb:
            if (e.get("proxyWallet") or "").lower() == address.lower():
                lb_entry = e
                break
    except Exception:
        pass

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        existing = await trader_repo.get_by_address(address)

        if existing:
            existing.is_pinned = True
            existing.status = "active"
            await session.flush()
            name = existing.display_name or address[:16] + "…"
            await update.message.reply_text(
                f"📌 <b>{name}</b> pinned and set to active.\n"
                f"Already in DB — will now never be auto-dropped.",
                parse_mode="HTML",
            )
        else:
            # Add them fresh
            display_name = lb_entry.get("userName") or target
            total_pnl = float(lb_entry.get("pnl", 0) or 0)
            new_trader = await trader_repo.upsert(
                address=address,
                display_name=display_name,
                score=0.5,  # Placeholder until discovery scores them
                status="active",
                total_pnl=total_pnl,
                leaderboard_rank=int(lb_entry.get("rank", 9999) or 9999) if lb_entry else None,
            )
            new_trader.is_pinned = True
            await session.flush()
            profile_url = f"https://polymarket.com/profile/{address}"
            pnl_str = f"+${total_pnl:,.0f}" if total_pnl > 0 else f"${total_pnl:,.0f}"
            await update.message.reply_text(
                f"📌 <b><a href=\"{profile_url}\">{display_name}</a></b> added and pinned!\n"
                f"💰 Leaderboard PnL: {pnl_str}\n"
                f"⚡ Now actively monitored. Stats will update on next discovery.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )


async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /untrack <address>. Unpin a trader (returns them to auto-management).
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /untrack <address>")
        return
    address = args[0].strip()
    async with get_session() as session:
        trader = await TraderRepo(session).unpin(address)
    if trader:
        name = trader.display_name or address[:16] + "…"
        await update.message.reply_text(f"🔓 <b>{name}</b> unpinned — discovery will auto-manage them.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Trader not found: {address[:16]}…")


async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /discover. Forces a fresh trader discovery run.
    """
    if not _is_admin(update):
        return
    await update.message.reply_text("🔍 Starting fresh trader discovery across all categories...\nThis takes a few minutes. I'll log progress.")
    try:
        from core.discovery import discover_top_traders
        await discover_top_traders()
        async with get_session() as session:
            trader_repo = TraderRepo(session)
            active = await trader_repo.get_active()
            all_traders = await trader_repo.get_all()
        watching = len(all_traders) - len(active)
        await update.message.reply_text(
            f"✅ Discovery complete!\n"
            f"🟢 <b>{len(active)}</b> traders actively monitored\n"
            f"🟡 <b>{watching}</b> traders in watching pool",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Discovery failed: %s", exc)
        await update.message.reply_text(f"❌ Discovery failed: {exc}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /help. Shows full command list.
    """
    if not _is_admin(update):
        return
    help_text = (
        "📖 <b>Commands</b>\n\n"
        "/start — Welcome & quick status\n"
        "/status — Full status with balance\n"
        "/mode &lt;auto|manual|paper&gt; — Switch trading mode\n"
        "/pause — Pause (set mode=manual)\n"
        "/resume — Resume auto trading\n"
        "/budget &lt;amount&gt; — Set total budget USD\n"
        "/pertrade &lt;pct&gt; — Set per-trade budget %\n"
        "/maxtrade &lt;usd&gt; — Set max trade size USD\n"
        "/traders — View tracked traders\n"
        "/positions — View open positions\n"
        "/history [n] — Last N closed positions\n"
        "/strategy list — All strategies + 7d P&L\n"
        "/strategy use &lt;slug&gt; — Switch strategy\n"
        "/signals — Last 20 signals across all tracked traders\n"
        "/report — Generate full report\n"
        "/settings — Show all settings\n"
        "/help — This message"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")
