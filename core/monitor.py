"""
Trade monitoring loop.
Polls tracked traders for new trades and emits validated signals.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.data_api import DataApiClient
from api.gamma import GammaApiClient
from api.clob import ClobApiClient
from config import get_settings
from db.models import Signal, Trader
from db.repos.positions import PositionRepo
from db.repos.signals import SignalRepo
from db.repos.traders import TraderRepo
from db.repos.settings import SettingsRepo
from db.session import get_session

logger = logging.getLogger(__name__)

_SPREAD_LIMIT = 0.10  # 10% max spread
_PRICE_STALENESS_MIN = 15  # Minutes before price considered stale
_RESOLVE_SOON_HOURS = 0   # Don't skip based on time — only skip truly closed markets


async def validate_signal(
    signal_data: dict,
    trader: Trader,
    clob_client: ClobApiClient,
    gamma_client: GammaApiClient,
    open_positions: list,
    settings: dict,
) -> tuple[bool, str]:
    """
    Validate a prospective signal before acting on it.

    Checks:
    1. Market is active (not closed/resolved).
    2. Market not resolving within 48 hours.
    3. Bid-ask spread < 10%.
    4. Price data not stale (< 15 minutes old).
    5. No existing open position for this market.

    Args:
        signal_data: Raw trade dict from Data API.
        trader: Trader ORM object.
        clob_client: Initialized ClobApiClient.
        gamma_client: Initialized GammaApiClient.
        open_positions: List of currently open Position objects.
        settings: Settings dict.

    Returns:
        (is_valid: bool, reason: str if invalid)
    """
    market_id = signal_data.get("market", {}).get("conditionId", "") if isinstance(signal_data.get("market"), dict) else signal_data.get("conditionId", "")
    token_id = signal_data.get("asset_id") or signal_data.get("asset") or signal_data.get("tokenId") or ""

    if not market_id:
        return False, "missing_market_id"

    # 1. Check market is not closed via CLOB (authoritative source)
    # We use CLOB closed flag — NOT Gamma endDate, which is often stale.
    # Many markets have endDate in the past but are still accepting orders (closed=False).
    try:
        import httpx as _httpx
        _r = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _httpx.get(
                f"https://clob.polymarket.com/markets/{market_id}", timeout=8
            )
        )
        if _r.status_code == 200:
            _m = _r.json()
            if _m.get("closed", False):
                return False, "market_already_resolved"
    except Exception as exc:
        logger.warning("Failed to check market closed status %s: %s", market_id, exc)

    # 3. Check spread
    try:
        spread = await clob_client.get_spread(token_id=token_id)
        if spread is not None and spread > _SPREAD_LIMIT:
            return False, f"spread_too_wide_{spread:.2%}"
    except Exception as exc:
        logger.warning("Spread check failed for token %s: %s", token_id, exc)

    # 4. Check price staleness (use trade timestamp)
    ts_str = signal_data.get("timestamp", signal_data.get("createdAt", ""))
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            if age_minutes > _PRICE_STALENESS_MIN:
                return False, f"price_stale_{age_minutes:.1f}min"
        except ValueError:
            pass

    # 5. Check no existing position in this market
    for pos in open_positions:
        if pos.market_condition_id == market_id:
            return False, "position_already_open"

    return True, ""


async def poll_trader(
    trader: Trader,
    last_trade_id: Optional[str],
    data_client: DataApiClient,
) -> list[dict]:
    """
    Fetch latest trades for a trader and return only new ones.

    Args:
        trader: Trader ORM object.
        last_trade_id: The last known trade ID for this trader (for dedup).
        data_client: Initialized DataApiClient.

    Returns:
        List of new raw trade dicts since last_trade_id.
    """
    try:
        trades = await data_client.get_trades(user=trader.address, limit=20)
    except Exception as exc:
        logger.error("Failed to fetch trades for trader %s: %s", trader.address, exc)
        return []

    if not trades:
        return []

    # Return trades newer than last_trade_id
    if last_trade_id is None:
        # First poll — return only the most recent trade to avoid mass-copying history
        return trades[:1]

    new_trades = []
    for trade in trades:
        trade_id = str(trade.get("id", trade.get("transactionHash", "")))
        if trade_id == last_trade_id:
            break
        new_trades.append(trade)

    return new_trades


# In-memory state: last seen trade ID per trader
_last_trade_ids: dict[int, Optional[str]] = {}


async def poll_all_traders() -> None:
    """
    Main monitoring tick. Polls all active traders for new trades.

    For each new trade signal detected:
    1. Validates the signal.
    2. Creates a Signal record.
    3. If valid, hands off to the executor for copying.
    """
    settings_cfg = get_settings()

    async with get_session() as session:
        trader_repo = TraderRepo(session)
        traders = await trader_repo.get_active()

    if not traders:
        return

    clob_client = ClobApiClient(
        private_key=settings_cfg.private_key,
        relayer_api_key=settings_cfg.relayer_api_key,
        relayer_api_address=settings_cfg.relayer_api_address,
        signer_address=settings_cfg.signer_address,
        relayer_api_secret=settings_cfg.relayer_api_secret,
        relayer_api_passphrase=settings_cfg.relayer_api_passphrase,
        funder_address=settings_cfg.funder_address,
    )

    async with DataApiClient() as data_client, GammaApiClient() as gamma_client:
        async with get_session() as session:
            settings_repo = SettingsRepo(session)
            settings = await settings_repo.as_dict()
            position_repo = PositionRepo(session)
            open_positions = list(await position_repo.get_open())

        for trader in traders:
            last_id = _last_trade_ids.get(trader.id)
            new_trades = await poll_trader(trader, last_id, data_client)

            if not new_trades:
                continue

            logger.info(
                "Found %d new trades for trader %s",
                len(new_trades),
                trader.address,
            )

            # Update last seen ID
            first_trade = new_trades[0]
            _last_trade_ids[trader.id] = str(
                first_trade.get("id", first_trade.get("transactionHash", ""))
            )

            # On first poll (last_id was None): just set the watermark, don't process.
            # This avoids flooding notifications with stale trades from before bot start.
            if last_id is None:
                logger.debug("First poll for trader %s — watermark set, skipping processing", trader.address[:16])
                continue

            for trade in new_trades:
                await _process_trade(
                    trade=trade,
                    trader=trader,
                    clob_client=clob_client,
                    gamma_client=gamma_client,
                    open_positions=open_positions,
                    settings=settings,
                    settings_cfg=settings_cfg,
                )


async def _process_trade(
    trade: dict,
    trader: Trader,
    clob_client: ClobApiClient,
    gamma_client: GammaApiClient,
    open_positions: list,
    settings: dict,
    settings_cfg,
) -> None:
    """
    Process a single raw trade dict: validate, record, and optionally execute.

    Args:
        trade: Raw trade dict from Data API.
        trader: Trader who made the trade.
        clob_client: CLOB API client.
        gamma_client: Gamma API client.
        open_positions: Current open positions.
        settings: DB settings dict.
        settings_cfg: Pydantic Settings instance.
    """
    # Extract key fields
    # Data API returns flat fields: conditionId, title, outcome, outcomeIndex, asset, price, size
    # NOT nested market object or asset_id/tokenId.
    market_info = trade.get("market", {})
    if isinstance(market_info, dict) and market_info:
        market_id = market_info.get("conditionId", "")
        market_name = market_info.get("question", "Unknown Market")
        market_category = market_info.get("category", "UNKNOWN")
    else:
        market_id = trade.get("conditionId", "")
        market_name = trade.get("title", "Unknown Market")
        market_category = "UNKNOWN"

    event_slug = trade.get("eventSlug") or trade.get("slug") or ""

    # token_id: Data API doesn't return it — look up from CLOB via conditionId + outcomeIndex
    token_id = trade.get("asset_id") or trade.get("asset") or trade.get("tokenId") or ""
    outcome_index = int(trade.get("outcomeIndex", 0) or 0)

    if not token_id and market_id:
        # Fetch token_id from CLOB market endpoint
        try:
            import httpx as _httpx
            _resp = await asyncio.to_thread(
                lambda: _httpx.get(
                    f"https://clob.polymarket.com/markets/{market_id}",
                    timeout=10,
                ).json()
            )
            tokens = _resp.get("tokens", [])
            if tokens and outcome_index < len(tokens):
                token_id = tokens[outcome_index].get("token_id", "")
                if not market_name or market_name == "Unknown Market":
                    market_name = _resp.get("question", market_name)
            logger.debug("Resolved token_id from CLOB for %s[%d]: %s", market_id[:16], outcome_index, token_id[:20] if token_id else "NONE")
        except Exception as exc:
            logger.warning("Failed to resolve token_id from CLOB: %s", exc)

    side = "BUY" if trade.get("side", "").upper() in ("BUY", "LONG") else "SELL"
    price = float(trade.get("price", trade.get("avgPrice", 0)) or 0)
    size_raw = trade.get("value") or trade.get("size") or trade.get("amount") or 0
    size_usd = float(size_raw) * price if float(size_raw) > 0 else 0
    trade_id = str(trade.get("id", trade.get("transactionHash", "")))

    if not market_id or price <= 0:
        logger.debug("Skipping trade: no market_id or price<=0")
        return

    if not token_id:
        logger.warning("Skipping trade: could not resolve token_id for %s", market_id[:20])
        return

    is_valid, skip_reason = await validate_signal(
        signal_data=trade,
        trader=trader,
        clob_client=clob_client,
        gamma_client=gamma_client,
        open_positions=open_positions,
        settings=settings,
    )

    async with get_session() as session:
        signal_repo = SignalRepo(session)
        strategy_repo_session = session

        # Determine active strategy ID
        from db.repos.strategies import StrategyRepo
        strategy_repo = StrategyRepo(session)
        active_strategy = await strategy_repo.get_active()
        strategy_id = active_strategy.id if active_strategy else None

        signal = await signal_repo.create(
            trader_id=trader.id,
            market_condition_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            raw_trade_id=trade_id,
            action_taken=None,
            skip_reason=skip_reason if not is_valid else None,
            strategy_id=strategy_id,
        )

    if not is_valid:
        logger.debug("Signal skipped: %s", skip_reason)
        async with get_session() as session:
            signal_repo = SignalRepo(session)
            await signal_repo.update_action(signal.id, "skipped", skip_reason)

        # Notify via bot
        signal._trader_address = trader.address  # type: ignore[attr-defined]
        signal._event_slug = event_slug  # type: ignore[attr-defined]
        from bot.notifications import signal_detected
        from bot import notifications as notif
        await notif.send_notification(signal_detected(
            signal=signal,
            action="skipped",
            skip_reason=skip_reason,
            trader_name=trader.display_name or trader.address[:12] + "…",
            market_name=market_name,
            event_slug=event_slug,
        ))
        return

    # Enrich signal with extra context for executor + notifications
    signal.market_category = market_category  # type: ignore[attr-defined]
    signal.trader = trader  # type: ignore[attr-defined]
    signal._trader_address = trader.address  # type: ignore[attr-defined]
    signal._event_slug = event_slug  # type: ignore[attr-defined]

    # Hand off to executor
    from core import executor
    mode = settings.get("mode", "manual")
    await executor.execute_copy_trade(signal=signal, mode=mode)
