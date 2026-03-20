"""
Inline keyboard builders for Telegram bot replies.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def status_keyboard() -> InlineKeyboardMarkup:
    """
    Build the inline keyboard for the /status command.

    Returns:
        InlineKeyboardMarkup with a Refresh button.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="status_refresh")],
    ])


def mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """
    Build the inline keyboard for mode selection.

    Args:
        current_mode: The currently active mode string.

    Returns:
        InlineKeyboardMarkup with Auto/Manual/Paper buttons.
    """
    def _label(mode: str) -> str:
        return f"✅ {mode.title()}" if mode == current_mode else mode.title()

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_label("auto"), callback_data="mode_auto"),
            InlineKeyboardButton(_label("manual"), callback_data="mode_manual"),
            InlineKeyboardButton(_label("paper"), callback_data="mode_paper"),
        ],
    ])


def traders_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """
    Build pagination keyboard for the /traders list.

    Args:
        page: Current page (1-indexed).
        total_pages: Total number of pages.

    Returns:
        InlineKeyboardMarkup with prev/next navigation.
    """
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"traders_page_{page - 1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"traders_page_{page + 1}"))
    if buttons:
        return InlineKeyboardMarkup([buttons])
    return InlineKeyboardMarkup([])


def positions_refresh_keyboard() -> InlineKeyboardMarkup:
    """
    Build a refresh button for the /positions command.

    Returns:
        InlineKeyboardMarkup with Refresh button.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="positions_refresh")],
    ])
