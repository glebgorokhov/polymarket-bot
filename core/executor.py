"""
Trade execution engine.
Handles copying trades, closing positions, stop-loss checks, and price updates.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from api.clob import ClobApiClient
from api.gamma import GammaApiClient
from config import get_settings
from core import risk
from db.models import Position, Signal
from db.repos.positions import ExecutionRepo, PositionRepo
from db.repos.settings import SettingsRepo
from db.repos.signals import SignalRepo
from db.repos.strategies import StrategyRepo
from db.session import get_session

logger = logging.getLogger(__name__)


def _get_clob_client() -> ClobApiClient:
    """Instantiate a CLOB client from current config."""
    cfg = get_settings()
    return ClobApiClient(
        private_key=cfg.private_key,
        relayer_api_key=cfg.relayer_api_key,
        relayer_api_address=cfg.relayer_api_address,
        signer_address=cfg.signer_address,
        relayer_api_secret=cfg.relayer_api_secret,
        relayer_api_passphrase=cfg.relayer_api_passphrase,
        funder_address=cfg.funder_address,
    )


async def execute_copy_trade(signal: Signal, mode: str) -> None:
    """
    Evaluate ALL active strategies for this signal simultaneously.

    Behaviour by mode:
    - auto:   Primary strategy places real order. Others create shadow positions.
    - paper:  Primary strategy creates paper position (no real order). Others shadow.
    - manual: No positions created. ONE notification sent listing all strategies
              that wanted to copy. User decides manually.

    In every mode, if ANY strategy triggers, a notification is sent listing which
    strategies fired and the market/side/price details.

    Args:
        signal: Validated Signal ORM object.
        mode: "auto", "paper", or "manual".
    """
    from core.strategies import get_all_active_strategies

    cfg = get_settings()

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        settings = await settings_repo.as_dict()
        primary_slug = await settings_repo.get("active_strategy_slug", "consensus")

    # Get balance
    clob_client = _get_clob_client()
    try:
        balance = await clob_client.get_balance()
    except Exception as exc:
        logger.error("Failed to get balance: %s", exc)
        balance = float(settings.get("budget_total", cfg.default_budget_total))

    per_trade_pct = float(settings.get("budget_per_trade_pct", cfg.default_per_trade_pct))
    max_trade_usd = float(settings.get("max_trade_usd", cfg.default_max_trade_usd))

    all_strategies = await get_all_active_strategies()
    if not all_strategies:
        logger.warning("No active strategies found")
        return

    # Shared context for all strategies
    async with get_session() as session:
        signal_repo = SignalRepo(session)
        recent_signals = list(await signal_repo.get_recent(hours=1))
        position_repo = PositionRepo(session)
        open_positions = list(await position_repo.get_open())

    market_name = getattr(signal, "market_name", None) or signal.market_condition_id

    # ── Pass 1: evaluate all strategies, collect those that say YES ──────────
    triggered: list[tuple[Any, Any, float]] = []  # (strat_orm, strategy, trade_size)
    for strat_orm, strategy in all_strategies:
        try:
            should_copy, conviction = await strategy.should_copy(
                signal=signal,
                all_recent_signals=recent_signals,
                open_positions=open_positions,
            )
        except Exception as exc:
            logger.error("Strategy %s error: %s", strat_orm.slug, exc)
            continue

        if not should_copy:
            logger.debug("Strategy %s declined signal %s", strat_orm.slug, signal.id)
            continue

        trade_size = risk.calculate_trade_size(
            available_balance=balance,
            per_trade_pct=per_trade_pct,
            max_trade_usd=max_trade_usd,
            conviction_multiplier=conviction,
        )
        if trade_size < 1.0:
            continue

        triggered.append((strat_orm, strategy, trade_size))

    if not triggered:
        # No strategy fired — mark skipped silently
        async with get_session() as session:
            await SignalRepo(session).update_action(signal.id, "skipped", "no_strategy_triggered")
        logger.debug("Signal %s: no strategy triggered", signal.id)
        return

    triggered_slugs = [strat_orm.slug for strat_orm, _, _ in triggered]
    logger.info(
        "Signal %s: %d strategies triggered (%s) — mode=%s",
        signal.id, len(triggered_slugs), ", ".join(triggered_slugs), mode,
    )

    # Store triggered strategy slugs on signal
    async with get_session() as session:
        await SignalRepo(session).update_strategies_triggered(signal.id, triggered_slugs, market_name)

    # ── Manual mode: notify and stop — user decides ──────────────────────────
    if mode == "manual":
        async with get_session() as session:
            await SignalRepo(session).update_action(signal.id, "manual")
        from bot.notifications import signal_detected_manual, send_notification
        await send_notification(signal_detected_manual(signal, triggered_slugs, market_name))
        return

    # ── Auto / paper: create positions ───────────────────────────────────────
    order_id: Optional[str] = None
    primary_position: Optional[Position] = None

    for strat_orm, strategy, trade_size in triggered:
        is_primary = (strat_orm.slug == primary_slug)
        shares = trade_size / signal.price if signal.price > 0 else 0

        # Risk check only for primary
        if is_primary:
            ok, risk_reason = await risk.check_risk_limits(
                market_condition_id=signal.market_condition_id,
                proposed_size=trade_size,
                open_positions=open_positions,
                total_balance=balance,
                settings=settings,
            )
            if not ok:
                logger.info("Risk check failed for primary: %s", risk_reason)
                async with get_session() as session:
                    await SignalRepo(session).update_action(signal.id, "skipped", f"risk:{risk_reason}")
                from bot.notifications import risk_alert, send_notification
                await send_notification(risk_alert("risk_limit", risk_reason))
                is_primary = False  # fall through as shadow

        if is_primary and mode == "auto":
            try:
                order_result = await clob_client.place_market_order(
                    token_id=signal.token_id,
                    side=signal.side,
                    amount=trade_size,
                )
                order_id = order_result.get("orderID", order_result.get("id", f"order_{signal.id}"))
                filled_price = float(order_result.get("price", signal.price) or signal.price)
                shares = trade_size / filled_price if filled_price > 0 else shares
                logger.info("Order placed: %s %s size=%.2f id=%s", signal.side, signal.token_id, trade_size, order_id)
            except Exception as exc:
                logger.error("Order placement failed: %s", exc)
                from bot.notifications import error_alert, send_notification
                await send_notification(error_alert(str(exc)))
                is_primary = False  # save as shadow on failure

        is_shadow = not is_primary
        async with get_session() as session:
            position_repo = PositionRepo(session)
            execution_repo = ExecutionRepo(session)
            position = await position_repo.create(
                market_condition_id=signal.market_condition_id,
                token_id=signal.token_id,
                market_name=market_name,
                side=signal.side,
                entry_price=signal.price,
                size_usd=trade_size,
                shares=shares,
                strategy_id=strat_orm.id,
                signal_id=signal.id,
                entry_cost=trade_size,
                is_shadow=is_shadow,
            )
            if not is_shadow:
                _order_id = order_id if mode == "auto" else f"paper_{signal.id}"
                await execution_repo.create(
                    position_id=position.id,
                    side=signal.side,
                    price=signal.price,
                    size=shares,
                    order_id=_order_id,
                    fee=0.0,
                )
            if is_primary:
                primary_position = position

        tag = "primary" if is_primary else f"shadow/{strat_orm.slug}"
        logger.info("Position %d [%s] %s %s @ %.4f", position.id, tag, signal.side, market_name[:40], signal.price)

    # Mark signal action
    action = "copied" if mode == "auto" else "paper"
    async with get_session() as session:
        await SignalRepo(session).update_action(signal.id, action)

    # One consolidated notification
    if primary_position:
        from bot.notifications import trade_opened_multi, send_notification
        await send_notification(trade_opened_multi(primary_position, triggered_slugs, mode))


async def close_position(position: Position, reason: str) -> None:
    """
    Close an open position: record exit, notify.

    Args:
        position: Open Position ORM object to close.
        reason: Human-readable close reason (e.g., "trader_exited", "stop_loss").
    """
    clob_client = _get_clob_client()

    try:
        current_price = await clob_client.get_midpoint(token_id=position.token_id)
    except Exception as exc:
        logger.warning("Failed to get midpoint for close: %s", exc)
        current_price = position.current_price or position.entry_price

    if not current_price or current_price <= 0:
        current_price = position.entry_price

    exit_value = (position.shares or 0) * current_price

    async with get_session() as session:
        position_repo = PositionRepo(session)
        execution_repo = ExecutionRepo(session)
        updated_position = await position_repo.close_position(
            position_id=position.id,
            exit_value=exit_value,
            close_reason=reason,
        )
        if updated_position and position.shares:
            await execution_repo.create(
                position_id=position.id,
                side="SELL",
                price=current_price,
                size=position.shares,
                fee=0.0,
            )

    logger.info("Position %d closed: reason=%s exit_value=%.2f", position.id, reason, exit_value)

    if updated_position:
        from bot.notifications import trade_closed, send_notification
        await send_notification(trade_closed(updated_position))


async def check_stop_losses() -> None:
    """Scan all open positions and close any that breach the stop-loss threshold."""
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        stop_loss_pct = await settings_repo.get_float("stop_loss_pct", 35.0)
        position_repo = PositionRepo(session)
        open_positions = list(await position_repo.get_open())

    if not open_positions:
        return

    clob_client = _get_clob_client()

    for position in open_positions:
        try:
            current_price = await clob_client.get_midpoint(token_id=position.token_id)
            if current_price is None:
                continue
            entry_cost = position.entry_cost or position.size_usd
            current_value = (position.shares or 0) * current_price
            loss_pct = ((current_value - entry_cost) / entry_cost) * 100.0 if entry_cost > 0 else 0
            if loss_pct <= -stop_loss_pct:
                logger.info("Stop loss triggered for position %d (loss=%.1f%%)", position.id, loss_pct)
                await close_position(position, reason=f"stop_loss_{abs(loss_pct):.1f}pct")
        except Exception as exc:
            logger.error("Error checking stop loss for position %d: %s", position.id, exc)


async def update_position_prices() -> None:
    """Refresh current_price for all open positions from CLOB midpoint."""
    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        settings = await settings_repo.as_dict()
        strategy_repo = StrategyRepo(session)
        active_strategy_orm = await strategy_repo.get_active()
        position_repo = PositionRepo(session)
        open_positions = list(await position_repo.get_open())

    if not open_positions:
        return

    clob_client = _get_clob_client()

    strategy = None
    if active_strategy_orm:
        from core.strategies import get_strategy
        try:
            strategy = get_strategy(active_strategy_orm.slug, active_strategy_orm.params or {})
        except ValueError:
            pass

    for position in open_positions:
        try:
            current_price = await clob_client.get_midpoint(token_id=position.token_id)
            if current_price is None:
                continue
            async with get_session() as session:
                await PositionRepo(session).update_current_price(position.id, current_price)
            if strategy:
                should_exit, exit_reason = await strategy.should_exit(
                    position=position,
                    current_price=current_price,
                    original_trader_exited=False,
                )
                if should_exit:
                    await close_position(position, reason=exit_reason)
        except Exception as exc:
            logger.error("Error updating price for position %d: %s", position.id, exc)
