"""
Telegram bot application factory.
Creates and configures the Application with all handlers registered.
"""

import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler  # noqa: F401

from bot.handlers.callbacks import handle_callback
from bot.handlers.commands import (
    cmd_budget,
    cmd_discover,
    cmd_feed,
    cmd_help,
    cmd_track,
    cmd_untrack,
    cmd_history,
    cmd_maxtrade,
    cmd_mode,
    cmd_pause,
    cmd_pertrade,
    cmd_positions,
    cmd_report,
    cmd_resume,
    cmd_settings,
    cmd_signals,
    cmd_simulate,
    cmd_start,
    cmd_status,
    cmd_strategy,
    cmd_traders,
)
from bot.notifications import set_application
from config import get_settings

logger = logging.getLogger(__name__)


def create_app() -> Application:
    """
    Create and configure the Telegram bot Application.

    Registers all command and callback handlers.
    Stores reference in notifications module for outbound messages.

    Returns:
        Configured Application instance ready for polling.
    """
    cfg = get_settings()
    app = Application.builder().token(cfg.telegram_bot_token).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("pertrade", cmd_pertrade))
    app.add_handler(CommandHandler("maxtrade", cmd_maxtrade))
    app.add_handler(CommandHandler("traders", cmd_traders))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("feed", cmd_feed))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("discover", cmd_discover))
    app.add_handler(CommandHandler("simulate", cmd_simulate))
    app.add_handler(CommandHandler("help", cmd_help))

    # Register inline callback handler
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Store app reference in notifications module
    set_application(app)

    logger.info("Telegram bot application configured with %d handlers", 17)
    return app
