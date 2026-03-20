"""
APScheduler job definitions.
All scheduled background tasks are defined here.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton scheduler instance, creating it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def generate_and_send_report() -> None:
    """
    Generate and send the 6-hour periodic report to the admin.

    Gathers metrics for the past 6 hours and formats them using
    the report_6h notification formatter.
    """
    logger.info("Generating 6h report")
    try:
        from datetime import datetime, timedelta, timezone

        from api.clob import ClobApiClient
        from bot import notifications as notif
        from bot.notifications import report_6h
        from config import get_settings
        from db.repos.positions import PositionRepo
        from db.repos.settings import SettingsRepo
        from db.repos.signals import SignalRepo
        from db.repos.strategies import StrategyRepo
        from db.repos.traders import TraderRepo
        from db.session import get_session

        cfg = get_settings()
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(hours=6)

        # Fetch balance
        try:
            clob = ClobApiClient(
                relayer_api_key=cfg.relayer_api_key,
                relayer_api_address=cfg.relayer_api_address,
                signer_address=cfg.signer_address,
            )
            balance = await clob.get_balance()
        except Exception:
            balance = 0.0

        async with get_session() as session:
            position_repo = PositionRepo(session)
            open_positions = list(await position_repo.get_open())
            deployed = sum(p.size_usd for p in open_positions)
            closed_period = list(await position_repo.get_closed_in_period(period_start, now))
            period_pnl = sum(p.pnl or 0 for p in closed_period)
            alltime_pnl = await position_repo.get_total_pnl()

            total = balance + deployed
            period_pnl_pct = (period_pnl / total * 100) if total > 0 else 0
            alltime_pnl_pct = (alltime_pnl / total * 100) if total > 0 else 0

            signal_repo = SignalRepo(session)
            signal_counts = await signal_repo.count_in_period(period_start, now)

            strategy_repo = StrategyRepo(session)
            strategies = list(await strategy_repo.get_all())
            strategy_perf = []
            letter_map = {
                "pure_follow": "A",
                "consensus": "B",
                "whale": "C",
                "category_expert": "D",
                "smart_exit": "E",
            }
            for strat in strategies:
                pnl_7d = await strategy_repo.get_7d_pnl(strat.id)
                strategy_perf.append({
                    "name": strat.name,
                    "pnl_7d": pnl_7d,
                    "is_active": strat.is_active,
                    "letter": letter_map.get(strat.slug, "?"),
                })

            trader_repo = TraderRepo(session)
            active_traders_count = await trader_repo.count_active()

        metrics = {
            "period_start": period_start,
            "period_end": now,
            "balance": balance,
            "deployed": deployed,
            "period_pnl": period_pnl,
            "period_pnl_pct": period_pnl_pct,
            "alltime_pnl": alltime_pnl,
            "alltime_pnl_pct": alltime_pnl_pct,
            "open_positions": open_positions,
            "closed_positions": closed_period,
            "strategy_performance": strategy_perf,
            "signals_detected": signal_counts.get("detected", 0),
            "signals_copied": signal_counts.get("copied", 0),
            "signals_skipped": signal_counts.get("skipped", 0),
            "active_traders_count": active_traders_count,
        }

        report_text = report_6h(metrics)

        # Save report to DB
        async with get_session() as session:
            from db.models import Report
            report = Report(
                period_start=period_start,
                period_end=now,
                report_text=report_text,
                metrics_json={
                    "period_pnl": period_pnl,
                    "alltime_pnl": alltime_pnl,
                    "active_traders": active_traders_count,
                    "signals": signal_counts,
                },
            )
            session.add(report)

        await notif.send_notification(report_text)
        logger.info("6h report sent successfully")

    except Exception as exc:
        logger.error("Failed to generate report: %s", exc)
        from bot import notifications as notif
        from bot.notifications import error_alert
        await notif.send_notification(error_alert(f"Report generation failed: {exc}"))


def start_scheduler() -> AsyncIOScheduler:
    """
    Configure and start the APScheduler with all background jobs.

    Jobs:
    - Every 30s: poll all traders for new signals
    - Every 30s: check stop losses on open positions
    - Every 1h: update current prices for open positions
    - Every 6h: generate and send report
    - Daily 02:00 UTC: refresh tracked traders
    - Sunday 03:00 UTC: discover new top traders

    Returns:
        Running AsyncIOScheduler instance.
    """
    scheduler = get_scheduler()

    from core import monitor, executor
    from core.discovery import refresh_tracked_traders, discover_top_traders

    # Poll traders every 30 seconds
    scheduler.add_job(
        monitor.poll_all_traders,
        trigger=IntervalTrigger(seconds=30),
        id="poll_traders",
        name="Poll tracked traders for new trades",
        replace_existing=True,
        max_instances=1,
    )

    # Check stop losses every 30 seconds
    scheduler.add_job(
        executor.check_stop_losses,
        trigger=IntervalTrigger(seconds=30),
        id="check_stop_losses",
        name="Check open positions for stop loss",
        replace_existing=True,
        max_instances=1,
    )

    # Update position prices every hour
    scheduler.add_job(
        executor.update_position_prices,
        trigger=IntervalTrigger(hours=1),
        id="update_prices",
        name="Update current prices for open positions",
        replace_existing=True,
        max_instances=1,
    )

    # Generate 6h report
    scheduler.add_job(
        generate_and_send_report,
        trigger=IntervalTrigger(hours=6),
        id="report_6h",
        name="Generate and send 6h performance report",
        replace_existing=True,
        max_instances=1,
    )

    # Daily trader refresh at 02:00 UTC
    scheduler.add_job(
        refresh_tracked_traders,
        trigger=CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="refresh_traders",
        name="Refresh tracked trader scores",
        replace_existing=True,
    )

    # Weekly discovery on Sunday at 03:00 UTC
    scheduler.add_job(
        discover_top_traders,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="UTC"),
        id="discover_traders",
        name="Discover new top traders",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started with %d jobs", len(scheduler.get_jobs()))
    return scheduler
