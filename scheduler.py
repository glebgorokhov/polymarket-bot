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
    """Generate and send the 6-hour periodic report to the admin."""
    logger.info("Generating 6h report")
    try:
        import asyncio
        import httpx
        from datetime import datetime, timedelta, timezone

        from api.clob import ClobApiClient
        from bot import notifications as notif
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

        # ── Balance (real CLOB with full auth) ───────────────────────────────
        balance = 0.0
        try:
            clob = ClobApiClient(
                private_key=cfg.private_key,
                relayer_api_key=cfg.relayer_api_key,
                relayer_api_address=cfg.relayer_api_address,
                signer_address=cfg.signer_address,
                relayer_api_secret=cfg.relayer_api_secret,
                relayer_api_passphrase=cfg.relayer_api_passphrase,
                funder_address=cfg.funder_address,
            )
            balance = float(await clob.get_balance() or 0)
        except Exception as exc:
            logger.warning("Balance fetch failed for report: %s", exc)

        async with get_session() as session:
            position_repo = PositionRepo(session)
            # Real positions only
            real_open = list(await position_repo.get_open(is_shadow=False))
            deployed = sum(p.entry_cost or p.size_usd for p in real_open)

            # Closed this period (real only)
            all_closed_period = list(await position_repo.get_closed_in_period(period_start, now))
            closed_period = [p for p in all_closed_period if not p.is_shadow]
            period_pnl = sum(p.pnl or 0 for p in closed_period)

            # All-time P&L (real only) — query directly
            from sqlalchemy import select
            from db.models import Position as _Pos
            closed_real = (await session.execute(
                select(_Pos).where(_Pos.status == "closed", _Pos.is_shadow == False)
            )).scalars().all()
            alltime_pnl = sum(p.pnl or 0 for p in closed_real)

            # Signal counts: "executed" = copied + paper
            signal_repo = SignalRepo(session)
            signal_counts = await signal_repo.count_in_period(period_start, now)

            # Active strategy
            settings_repo = SettingsRepo(session)
            active_slug = await settings_repo.get("active_strategy_slug", "consensus")
            mode = await settings_repo.get("mode", "manual")

            # Strategies
            strategy_repo = StrategyRepo(session)
            strategies = list(await strategy_repo.get_all())

            trader_repo = TraderRepo(session)
            active_traders_count = await trader_repo.count_active()

        # ── Batch fetch market questions from CLOB ───────────────────────────
        unique_cids = list({p.market_condition_id for p in real_open + closed_period})
        clob_meta: dict[str, dict] = {}

        async def _fetch_meta(client, cid):
            try:
                r = await client.get(f"https://clob.polymarket.com/markets/{cid}", timeout=8)
                if r.status_code == 200:
                    clob_meta[cid] = r.json()
            except Exception:
                pass

        if unique_cids:
            async with httpx.AsyncClient() as hc:
                await asyncio.gather(*[_fetch_meta(hc, cid) for cid in unique_cids])

        def _question(pos) -> str:
            d = clob_meta.get(pos.market_condition_id) or {}
            q = d.get("question") or pos.market_name or ""
            if not q or q.startswith("0x"):
                q = pos.market_condition_id[:20] + "…"
            return q[:55] + "…" if len(q) > 55 else q

        def _outcome(pos) -> str:
            out = pos.outcome
            if not out:
                d = clob_meta.get(pos.market_condition_id) or {}
                for tok in (d.get("tokens") or []):
                    if tok.get("token_id") == pos.token_id:
                        out = tok.get("outcome")
                        break
            return out or ""

        # ── Build report text ────────────────────────────────────────────────
        start_str = period_start.strftime("%H:%M")
        end_str = now.strftime("%H:%M")
        period_sign = "+" if period_pnl >= 0 else ""
        alltime_sign = "+" if alltime_pnl >= 0 else ""

        lines = [
            f"📊 <b>Report {start_str}–{end_str} UTC</b>  ·  mode: <b>{mode.upper()}</b>",
            "",
            f"💰 Balance: <b>${balance:.2f}</b>  ·  deployed: <b>${deployed:.2f}</b>",
            f"📈 Period P&amp;L: <b>{period_sign}${period_pnl:.2f}</b>  ·  All-time: <b>{alltime_sign}${alltime_pnl:.2f}</b>",
        ]

        # ── Open positions grouped by market ─────────────────────────────────
        from collections import OrderedDict
        market_groups: dict[str, list] = OrderedDict()
        for p in real_open:
            cid = p.market_condition_id
            if cid not in market_groups:
                market_groups[cid] = []
            market_groups[cid].append(p)

        lines.append("")
        lines.append(f"📂 <b>Open</b> — {len(market_groups)} market{'s' if len(market_groups) != 1 else ''}, {len(real_open)} position{'s' if len(real_open) != 1 else ''}")

        for cid, positions in market_groups.items():
            d = clob_meta.get(cid) or {}
            question = _question(positions[0])
            slug = d.get("market_slug") or cid
            market_url = f"https://polymarket.com/event/{slug}"
            is_closed = d.get("closed", False)

            # Combined P&L
            total_cost = sum(p.entry_cost or p.size_usd for p in positions)
            total_value = sum((p.shares or 0) * (p.current_price or p.entry_price) for p in positions)
            combined_pct = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
            icon = "🟢" if combined_pct >= 0 else "🔴"
            sign = "+" if combined_pct >= 0 else ""
            resolved_tag = " · <i>✅ resolved</i>" if is_closed else ""

            if len(positions) == 1:
                p = positions[0]
                out = _outcome(p)
                out_str = f" → {out}" if out else ""
                lines.append(
                    f"{icon} <a href=\"{market_url}\">{question}</a>{out_str}{resolved_tag}"
                    f"  {sign}{combined_pct:.1f}%  (${total_cost:.2f}→${total_value:.2f})"
                )
            else:
                out = _outcome(positions[0])
                out_str = f" → {out}" if out else ""
                lines.append(
                    f"{icon} <a href=\"{market_url}\">{question}</a>{out_str}{resolved_tag}"
                    f"  ×{len(positions)}  {sign}{combined_pct:.1f}%  (${total_cost:.2f}→${total_value:.2f})"
                )

        # ── Closed this period ────────────────────────────────────────────────
        lines.append("")
        lines.append(f"📋 <b>Closed this period</b> ({len(closed_period)})")
        if closed_period:
            for p in closed_period:
                pnl = p.pnl or 0
                pnl_pct = p.pnl_pct or 0
                icon = "✅" if pnl >= 0 else "❌"
                sign = "+" if pnl >= 0 else ""
                q = _question(p)
                out = _outcome(p)
                out_str = f" → {out}" if out else ""
                lines.append(f"  {icon} {q}{out_str}  {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)")
        else:
            lines.append("  <i>None</i>")

        # ── Signals ───────────────────────────────────────────────────────────
        detected = signal_counts.get("detected", 0)
        executed = signal_counts.get("copied", 0) + signal_counts.get("paper", 0)
        skipped = signal_counts.get("skipped", 0)
        lines.append("")
        lines.append(
            f"📡 Signals: <b>{detected}</b> detected · <b>{executed}</b> executed · <b>{skipped}</b> skipped"
        )

        # ── Strategy summary ──────────────────────────────────────────────────
        slug_names = {
            "pure_follow": "Pure Follow",
            "consensus": "Consensus Gate",
            "whale": "Whale Entry",
            "category_expert": "Category Expert",
            "smart_exit": "Smart Exit",
        }
        active_name = slug_names.get(active_slug, active_slug)
        lines.append(f"🎯 Active strategy: <b>{active_name}</b>  ·  👥 <b>{active_traders_count}</b> traders")

        report_text = "\n".join(lines)

        # Save to DB
        async with get_session() as session:
            from db.models import Report
            session.add(Report(
                period_start=period_start,
                period_end=now,
                report_text=report_text,
                metrics_json={
                    "period_pnl": period_pnl,
                    "alltime_pnl": alltime_pnl,
                    "active_traders": active_traders_count,
                    "signals": signal_counts,
                },
            ))

        await notif.send_notification(report_text)
        logger.info("6h report sent successfully")

    except Exception as exc:
        logger.error("Failed to generate report: %s", exc, exc_info=True)
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
    from core.discovery import refresh_trader_scores, discover_top_traders

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

    # Check for resolved markets every 15 minutes — auto-close with correct P&L
    scheduler.add_job(
        executor.check_market_resolutions,
        trigger=IntervalTrigger(minutes=15),
        id="check_resolutions",
        name="Auto-close positions in resolved markets",
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
        refresh_trader_scores,
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
