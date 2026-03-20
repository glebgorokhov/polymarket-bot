"""
Notification formatters and sender for the Telegram bot.
All formatter functions return formatted message strings.
The send_notification() function dispatches to the bot.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Global reference to the telegram Application (set in app.py after init)
_application = None


def set_application(app: Any) -> None:
    """
    Register the Telegram Application instance for outbound notifications.

    Args:
        app: The python-telegram-bot Application instance.
    """
    global _application
    _application = app


async def send_notification(text: str) -> None:
    """
    Send a notification message to the admin chat.

    Args:
        text: Formatted message string to send.
    """
    if _application is None:
        logger.warning("Notification skipped (no application registered): %s", text[:80])
        return
    try:
        from config import get_settings
        cfg = get_settings()
        await _application.bot.send_message(
            chat_id=cfg.telegram_admin_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Failed to send notification: %s", exc)


def trader_added(trader: Any) -> str:
    """
    Format a message for when a new trader is added to tracking.

    Args:
        trader: Trader ORM object.

    Returns:
        Formatted notification string.
    """
    name = trader.display_name or trader.address[:10] + "..."
    return (
        f"🟢 <b>New trader tracked</b>\n"
        f"👤 {name}\n"
        f"📊 Score: {trader.score:.3f}\n"
        f"💰 Total PnL: ${trader.total_pnl:+.2f}\n"
        f"🔑 <code>{trader.address}</code>"
    )


def trader_removed(trader: Any, reason: str) -> str:
    """
    Format a message for when a trader is removed from tracking.

    Args:
        trader: Trader ORM object.
        reason: Why the trader was removed.

    Returns:
        Formatted notification string.
    """
    name = trader.display_name or trader.address[:10] + "..."
    return (
        f"🔴 <b>Trader removed</b>\n"
        f"👤 {name}\n"
        f"📝 Reason: {reason}\n"
        f"🔑 <code>{trader.address}</code>"
    )


def signal_detected_manual(signal: Any, triggered_slugs: list, market_name: str) -> str:
    """
    Notification for manual mode — lists all strategies that wanted to copy.
    User can decide whether to act manually.
    """
    side_icon = "🔵" if signal.side == "BUY" else "🔴"
    strat_lines = "\n".join(f"  · {slug}" for slug in triggered_slugs)
    return (
        f"⚠️ <b>Signal — manual review needed</b>\n"
        f"\n"
        f"{side_icon} <b>{market_name}</b>\n"
        f"📈 {signal.side} @ <b>{signal.price:.4f}</b> · ${signal.size_usd:.2f}\n"
        f"\n"
        f"Triggered by {len(triggered_slugs)} {'strategy' if len(triggered_slugs) == 1 else 'strategies'}:\n"
        f"{strat_lines}\n"
        f"\n"
        f"Switch to /mode paper or /mode auto to let me handle this."
    )


def trade_opened_multi(position: Any, triggered_slugs: list, mode: str) -> str:
    """
    Notification when a position is opened, showing all strategies that triggered.
    """
    mode_icon = "✅" if mode == "auto" else "📄"
    mode_label = "Trade placed" if mode == "auto" else "Paper trade"
    shadow_count = len(triggered_slugs) - 1
    primary_slug = triggered_slugs[0] if triggered_slugs else "unknown"

    shadow_note = (
        f"\n👁️ {shadow_count} more {'strategy' if shadow_count == 1 else 'strategies'} shadow-tracking: "
        + ", ".join(triggered_slugs[1:])
        if shadow_count > 0 else ""
    )

    return (
        f"{mode_icon} <b>{mode_label}</b>\n"
        f"\n"
        f"🏪 {position.market_name}\n"
        f"📈 {position.side} @ {position.entry_price:.4f} · ${position.size_usd:.2f}\n"
        f"🎯 Primary: <b>{primary_slug}</b>{shadow_note}\n"
        f"🆔 Position #{position.id}"
    )


def signal_detected(signal: Any, action: str, skip_reason: Optional[str] = None) -> str:
    """
    Format a message for a detected trade signal.

    Args:
        signal: Signal ORM object.
        action: "copied", "skipped", or "manual".
        skip_reason: Reason if skipped.

    Returns:
        Formatted notification string.
    """
    action_icons = {"copied": "✅", "skipped": "⏭️", "manual": "⚠️"}
    icon = action_icons.get(action, "📡")
    action_label = action.upper()

    msg = (
        f"{icon} <b>Signal {action_label}</b>\n"
        f"🏪 Market: <code>{signal.market_condition_id[:20]}...</code>\n"
        f"📈 Side: {signal.side} @ {signal.price:.4f}\n"
        f"💵 Size: ${signal.size_usd:.2f}"
    )
    if skip_reason:
        msg += f"\n⚠️ Reason: {skip_reason}"
    return msg


def trade_opened(position: Any, strategy_name: str) -> str:
    """
    Format a message for when a new position is opened.

    Args:
        position: Position ORM object.
        strategy_name: Name of the strategy that triggered the trade.

    Returns:
        Formatted notification string.
    """
    return (
        f"🟢 <b>Position Opened</b>\n"
        f"🏪 {position.market_name}\n"
        f"📈 {position.side} @ {position.entry_price:.4f}\n"
        f"💵 Size: ${position.size_usd:.2f}\n"
        f"🎯 Strategy: {strategy_name}\n"
        f"🆔 ID: {position.id}"
    )


def trade_closed(position: Any) -> str:
    """
    Format a message for when a position is closed.

    Args:
        position: Closed Position ORM object (has pnl, pnl_pct set).

    Returns:
        Formatted notification string.
    """
    pnl = position.pnl or 0.0
    pnl_pct = position.pnl_pct or 0.0
    pnl_icon = "✅" if pnl >= 0 else "❌"
    pnl_sign = "+" if pnl >= 0 else ""

    return (
        f"{pnl_icon} <b>Position Closed</b>\n"
        f"🏪 {position.market_name}\n"
        f"📈 {position.side} entry @ {position.entry_price:.4f}\n"
        f"💵 P&L: <b>{pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%)</b>\n"
        f"📝 Reason: {position.close_reason}\n"
        f"🆔 ID: {position.id}"
    )


def report_6h(metrics: dict) -> str:
    """
    Format the 6-hour periodic report.

    Args:
        metrics: Dict containing report metrics. Expected keys:
            period_start, period_end, balance, deployed, period_pnl,
            period_pnl_pct, alltime_pnl, alltime_pnl_pct,
            open_positions (list), closed_positions (list),
            strategy_performance (list of {name, pnl_7d, is_active, letter}),
            signals_detected, signals_copied, signals_skipped,
            active_traders_count.

    Returns:
        Formatted report string in the specified format.
    """
    start: datetime = metrics.get("period_start", datetime.now(timezone.utc))
    end: datetime = metrics.get("period_end", datetime.now(timezone.utc))

    start_str = start.strftime("%H:%M")
    end_str = end.strftime("%H:%M")

    balance = metrics.get("balance", 0.0)
    deployed = metrics.get("deployed", 0.0)
    period_pnl = metrics.get("period_pnl", 0.0)
    period_pnl_pct = metrics.get("period_pnl_pct", 0.0)
    alltime_pnl = metrics.get("alltime_pnl", 0.0)
    alltime_pnl_pct = metrics.get("alltime_pnl_pct", 0.0)

    period_sign = "+" if period_pnl >= 0 else ""
    alltime_sign = "+" if alltime_pnl >= 0 else ""

    lines = [
        f"📊 <b>Report — {start_str} – {end_str}</b>",
        "",
        f"💰 Balance: ${balance:.2f} available, ${deployed:.2f} deployed",
        f"📈 Period P&L: {period_sign}${period_pnl:.2f} ({period_sign}{period_pnl_pct:.1f}%)",
        f"📉 All-time: {alltime_sign}${alltime_pnl:.2f} ({alltime_sign}{alltime_pnl_pct:.1f}%)",
    ]

    # Open positions
    open_positions = metrics.get("open_positions", [])
    lines.append("")
    lines.append(f"Open positions ({len(open_positions)}):")
    for pos in open_positions:
        current = pos.current_price or pos.entry_price
        entry_cost = pos.entry_cost or pos.size_usd
        current_value = (pos.shares or 0) * current
        pos_pnl = current_value - entry_cost
        pos_pnl_pct = (pos_pnl / entry_cost * 100) if entry_cost > 0 else 0
        sign = "+" if pos_pnl >= 0 else ""
        token_label = "YES" if pos.side == "BUY" else "NO"
        market_short = pos.market_name[:30] + "..." if len(pos.market_name) > 30 else pos.market_name
        lines.append(
            f"  • {market_short} {token_label} @ {pos.entry_price:.2f} → ${current_value:.2f} ({sign}{pos_pnl_pct:.1f}%)"
        )

    # Closed this period
    closed_positions = metrics.get("closed_positions", [])
    lines.append("")
    lines.append(f"Closed this period ({len(closed_positions)}):")
    for pos in closed_positions:
        pnl = pos.pnl or 0
        pnl_pct = pos.pnl_pct or 0
        icon = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        market_short = pos.market_name[:30] + "..." if len(pos.market_name) > 30 else pos.market_name
        lines.append(f"  {icon} {market_short} {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)")

    # Strategy performance
    strategy_perf = metrics.get("strategy_performance", [])
    lines.append("")
    lines.append("Strategy 7d performance:")
    for strat in strategy_perf:
        pnl_7d = strat.get("pnl_7d", 0.0)
        is_active = strat.get("is_active", False)
        name = strat.get("name", "Unknown")
        letter = strat.get("letter", "?")
        sign = "+" if pnl_7d >= 0 else ""
        active_tag = " [active]" if is_active else ""
        icon = "🔵" if is_active else "⚪"
        lines.append(f"  {icon} {letter} {name}: {sign}{pnl_7d:.1f}%{active_tag}")

    # Signal stats
    detected = metrics.get("signals_detected", 0)
    copied = metrics.get("signals_copied", 0)
    skipped = metrics.get("signals_skipped", 0)
    trader_count = metrics.get("active_traders_count", 0)

    lines.append("")
    lines.append(f"Signals: {detected} detected, {copied} copied, {skipped} skipped")
    lines.append(f"Traders: {trader_count} active")

    return "\n".join(lines)


def risk_alert(alert_type: str, details: str) -> str:
    """
    Format a risk management alert.

    Args:
        alert_type: Type identifier (e.g., "risk_limit", "stop_loss").
        details: Human-readable details about the alert.

    Returns:
        Formatted alert string.
    """
    return (
        f"⚠️ <b>Risk Alert: {alert_type.upper()}</b>\n"
        f"📝 {details}"
    )


def low_balance(balance: float) -> str:
    """
    Format a low balance warning.

    Args:
        balance: Current available balance in USD.

    Returns:
        Formatted warning string.
    """
    return (
        f"⚠️ <b>Low Balance Warning</b>\n"
        f"💰 Available balance: ${balance:.2f}\n"
        f"Consider adding funds to continue trading."
    )


def error_alert(error: str) -> str:
    """
    Format a critical error notification.

    Args:
        error: Error message or exception string.

    Returns:
        Formatted error notification string.
    """
    # Truncate very long errors
    if len(error) > 400:
        error = error[:400] + "..."
    return (
        f"🚨 <b>Error Alert</b>\n"
        f"<code>{error}</code>"
    )
