"""
Inline keyboard callback handlers for the Telegram bot.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import get_settings
from db.repos.settings import SettingsRepo
from db.session import get_session

logger = logging.getLogger(__name__)


def _is_admin(update: Update) -> bool:
    """Check if the callback sender is the configured admin."""
    cfg = get_settings()
    user = update.effective_user
    return user is not None and user.id == cfg.telegram_admin_id


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route all inline keyboard callbacks to their specific handlers.

    Args:
        update: Telegram update with callback_query.
        context: Handler context.
    """
    if not _is_admin(update):
        await update.callback_query.answer()
        return

    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "status_refresh":
        await _cb_status_refresh(update, context)
    elif data.startswith("mode_"):
        await _cb_mode_switch(update, context, data.removeprefix("mode_"))
    elif data.startswith("traders_page_"):
        await _cb_traders_page(update, context, int(data.removeprefix("traders_page_")))
    elif data == "positions_refresh":
        await _cb_positions_refresh(update, context)
    else:
        logger.warning("Unknown callback data: %s", data)


async def _cb_status_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Refresh the status message inline.

    Args:
        update: Telegram update.
        context: Handler context.
    """
    from bot.handlers.commands import _build_status_text
    from bot.keyboards import status_keyboard

    text = await _build_status_text()
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=status_keyboard()
    )


async def _cb_mode_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str
) -> None:
    """
    Switch the trading mode via inline button.

    Args:
        update: Telegram update.
        context: Handler context.
        mode: Target mode string ("auto", "manual", "paper").
    """
    if mode not in ("auto", "manual", "paper"):
        return

    async with get_session() as session:
        settings_repo = SettingsRepo(session)
        await settings_repo.set("mode", mode)

    from bot.keyboards import mode_keyboard

    mode_icons = {"auto": "🟢", "manual": "🟡", "paper": "📝"}
    await update.callback_query.edit_message_text(
        f"{mode_icons[mode]} Mode switched to <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=mode_keyboard(mode),
    )


async def _cb_traders_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    """
    Navigate to a traders list page.

    Args:
        update: Telegram update.
        context: Handler context.
        page: Page number to display.
    """
    from bot.handlers.commands import _send_traders_page

    await _send_traders_page(update, page)


async def _cb_positions_refresh(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Refresh the open positions message inline.

    Args:
        update: Telegram update.
        context: Handler context.
    """
    from bot.handlers.commands import _build_positions_text
    from bot.keyboards import positions_refresh_keyboard

    text = await _build_positions_text()
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=positions_refresh_keyboard()
    )
