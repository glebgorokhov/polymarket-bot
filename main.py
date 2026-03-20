"""
Polymarket Copytrading Bot — Main Entrypoint.

Startup sequence:
1. Load config
2. Initialize DB engine
3. Run Alembic migrations (upgrade head)
4. Seed default strategies if table empty
5. Seed default settings if table empty
6. Start APScheduler
7. If no traders in DB: run initial discover_top_traders()
8. Start Telegram bot polling
9. Handle SIGTERM gracefully
"""

import asyncio
import logging
import signal
import sys

from config import get_settings
from db.session import init_engine, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Default strategy seed data
_DEFAULT_STRATEGIES = [
    {
        "slug": "pure_follow",
        "name": "Pure Follow",
        "description": "Copy every trade proportionally. Exit when trader exits.",
        "params": {},
        "is_active": False,
    },
    {
        "slug": "consensus",
        "name": "Consensus Gate",
        "description": "Only trade when 2+ tracked traders enter the same market.",
        "params": {"min_traders": 2, "window_minutes": 10},
        "is_active": True,
    },
    {
        "slug": "whale",
        "name": "Whale Entry",
        "description": "Only copy when trader's bet exceeds 2x their average size.",
        "params": {"size_multiplier": 2.0},
        "is_active": False,
    },
    {
        "slug": "category_expert",
        "name": "Category Expert",
        "description": "Copy traders only within their strongest category.",
        "params": {"min_strength": 0.6},
        "is_active": False,
    },
    {
        "slug": "smart_exit",
        "name": "Smart Exit",
        "description": "Trail the position instead of blindly exiting with trader.",
        "params": {"trail_stop_pct": 25, "take_profit_prob": 0.85},
        "is_active": False,
    },
]

# Default settings seed data
_DEFAULT_SETTINGS = {
    "mode": "manual",
    "budget_total": "50.0",
    "budget_per_trade_pct": "5.0",
    "max_trade_usd": "20.0",
    "active_strategy_slug": "consensus",
    "max_total_exposure_pct": "60.0",
    "per_market_cap_pct": "20.0",
    "stop_loss_pct": "35.0",
    "max_drawdown_pct": "15.0",
    "conviction_multiplier_2": "1.5",
    "conviction_multiplier_3": "2.0",
}


async def run_migrations() -> None:
    """Run Alembic migrations to bring the DB schema up to date."""
    logger.info("Running Alembic migrations...")
    import subprocess
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Alembic migration failed:\n%s", result.stderr)
        raise RuntimeError("DB migration failed")
    logger.info("Migrations complete")


async def seed_strategies() -> None:
    """Insert default strategies if the strategies table is empty."""
    from db.repos.strategies import StrategyRepo

    async with get_session() as session:
        repo = StrategyRepo(session)
        count = await repo.count()
        if count > 0:
            logger.info("Strategies already seeded (%d found)", count)
            return

        for data in _DEFAULT_STRATEGIES:
            await repo.create(
                name=data["name"],
                slug=data["slug"],
                description=data["description"],
                params=data["params"],
                is_active=data["is_active"],
            )
        logger.info("Seeded %d default strategies", len(_DEFAULT_STRATEGIES))


async def seed_settings() -> None:
    """Insert default settings if the settings table is empty."""
    from db.repos.settings import SettingsRepo

    async with get_session() as session:
        repo = SettingsRepo(session)
        count = await repo.count()
        if count > 0:
            logger.info("Settings already seeded (%d found)", count)
            return

        for key, value in _DEFAULT_SETTINGS.items():
            await repo.set(key, value)
        logger.info("Seeded %d default settings", len(_DEFAULT_SETTINGS))


async def maybe_discover_traders() -> None:
    """Run initial trader discovery if no traders exist in the DB."""
    from db.repos.traders import TraderRepo

    async with get_session() as session:
        repo = TraderRepo(session)
        count = await repo.count_all()

    if count == 0:
        logger.info("No traders in DB — running initial discovery")
        from core.discovery import discover_top_traders
        await discover_top_traders()
    else:
        logger.info("Found %d traders in DB — skipping initial discovery", count)


async def main() -> None:
    """Main async entry point."""
    cfg = get_settings()
    logger.info("Starting Polymarket Copytrading Bot")

    # 1. Initialize DB engine
    engine = init_engine()
    logger.info("DB engine initialized")

    # 2. Run migrations
    await run_migrations()

    # 3. Seed data
    await seed_strategies()
    await seed_settings()

    # 4. Start APScheduler
    from scheduler import start_scheduler
    scheduler = start_scheduler()

    # 5. Run initial discovery if needed
    try:
        await maybe_discover_traders()
    except Exception as exc:
        logger.warning("Initial trader discovery failed: %s — continuing without", exc)

    # 6. Create Telegram bot
    from bot.app import create_app
    app = create_app()

    # 7. Graceful shutdown handler
    stop_event = asyncio.Event()

    def _handle_sigterm(*_) -> None:
        logger.info("SIGTERM received — initiating graceful shutdown")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    # 8. Start bot polling
    logger.info("Starting Telegram bot polling")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot is running. Press Ctrl+C to stop.")

        # Wait for shutdown signal
        await stop_event.wait()

        logger.info("Shutting down...")
        await app.updater.stop()
        await app.stop()

    # 9. Stop scheduler
    scheduler.shutdown(wait=False)
    await engine.dispose()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
