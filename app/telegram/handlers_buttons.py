import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from app.telegram.auth import reject_if_unauthorized
from app.telegram.cleanup_files import delete_all_files
from app.telegram.handlers_approve import handle_approve, handle_deny
from app.telegram.handlers_approve_meta import handle_approve_fb, handle_approve_ig
from app.telegram.handlers_generate import run_ai_generation, run_timestamp_generation
from app.telegram.handlers_start import ask_subtitles

logger = logging.getLogger(__name__)


async def telegram_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update):
        return
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("Ignoring stale callback answer: %s", exc)

    data = query.data

    if data == "cancel":
        context.user_data.clear()
        try:
            await query.edit_message_text(
                "❌ Cancelled.\n\nSend /start to open the menu."
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram cancel edit failed: %s", exc)
        return

    if data == "create_shorts":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("🤖 AI Viral Clips", callback_data="mode_ai")],
            [InlineKeyboardButton("⏱️ Timestamp Clip", callback_data="mode_timestamp")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
        ]
        try:
            await query.edit_message_text(
                "🎬 Create Shorts\n\nChoose the clip type:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram create_shorts edit failed: %s", exc)
        return

    if data == "delete_menu":
        keyboard = [[
            InlineKeyboardButton("🗑️ DELETE ALL", callback_data="delete_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="cancel"),
        ]]
        try:
            await query.edit_message_text(
                "⚠️ Delete all saved files?\n\n"
                "This removes downloaded videos, generated Shorts, "
                "and temporary files. The folders remain.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram delete_menu edit failed: %s", exc)
        return

    if data == "delete_confirm":
        try:
            await query.edit_message_text("🗑️ Deleting files...")
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram delete_confirm edit failed: %s", exc)
        try:
            count, freed = await delete_all_files()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "✅ Cleanup complete!\n\n"
                    f"🗑️ Items deleted: {count}\n"
                    f"💾 Space freed: {freed / (1024 * 1024):.2f} MB"
                ),
            )
        except Exception as e:
            logger.exception("Telegram cleanup failed")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Cleanup failed.\n\nError: {str(e)}",
            )
        return

    if data == "mode_ai":
        context.user_data["mode"] = "ai"
        try:
            await query.edit_message_text(
                "🤖 AI Viral Clips\n\nNow send the YouTube video URL."
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram mode_ai edit failed: %s", exc)
        return

    if data == "mode_timestamp":
        context.user_data["mode"] = "timestamp"
        try:
            await query.edit_message_text(
                "⏱️ Timestamp Clip\n\nNow send the YouTube video URL."
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram mode_timestamp edit failed: %s", exc)
        return

    if data.startswith("count:"):
        value = data.split(":", 1)[1]
        context.user_data["max_clips"] = None if value == "ai" else int(value)
        await ask_subtitles(query, context)
        return

    if data.startswith("subs:"):
        context.user_data["subtitles"] = data.split(":", 1)[1]

        if context.user_data.get("mode") == "ai":
            await run_ai_generation(query, context)
        elif context.user_data.get("mode") == "timestamp":
            await run_timestamp_generation(query, context)
        else:
            await query.edit_message_text("❌ No active mode. Send /start.")
        return

    if data.startswith("approve_ig:"):
        await handle_approve_ig(update, context, data.split(":", 1)[1])
        return

    if data.startswith("approve_fb:"):
        await handle_approve_fb(update, context, data.split(":", 1)[1])
        return

    if data.startswith("approve:"):
        await handle_approve(update, context, data.split(":", 1)[1])
        return

    if data.startswith("deny:"):
        await handle_deny(update, context, data.split(":", 1)[1])
        return
