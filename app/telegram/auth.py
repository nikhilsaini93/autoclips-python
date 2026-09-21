import logging

from app.config import settings

logger = logging.getLogger(__name__)


def allowed_chat_ids() -> set:
    return settings.allowed_chat_ids()


def is_chat_allowed(chat_id) -> bool:
    allowed = allowed_chat_ids()
    if not allowed:
        return True
    return str(chat_id) in allowed


async def reject_if_unauthorized(update) -> bool:
    """Returns True if the update was rejected (and a notice sent)."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if is_chat_allowed(chat_id):
        return False
    logger.warning("Rejected unauthorized Telegram chat_id=%s", chat_id)
    try:
        if update.message:
            await update.message.reply_text("⛔ Unauthorized. This bot is private.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
    except Exception:
        pass
    return True
