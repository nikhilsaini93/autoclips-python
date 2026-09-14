import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.services.video.timeutils import time_to_seconds
from app.telegram.auth import reject_if_unauthorized

logger = logging.getLogger(__name__)


async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update):
        return
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🎬 Create Shorts", callback_data="create_shorts")],
        [InlineKeyboardButton("🗑️ Delete Files", callback_data="delete_menu")],
    ]

    await update.message.reply_text(
        "🤖 AutoClips Bot\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def telegram_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not ("youtube.com/" in url or "youtu.be/" in url):
        await update.message.reply_text("❌ Please send a valid YouTube URL.")
        return

    mode = context.user_data.get("mode")

    # Prevent the old behavior: URL is accepted only after a mode is selected.
    if mode not in ("ai", "timestamp"):
        await update.message.reply_text(
            "Please choose an option first:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Create Shorts", callback_data="create_shorts")],
                [InlineKeyboardButton("🗑️ Delete Files", callback_data="delete_menu")],
            ]),
        )
        return

    context.user_data["youtube_url"] = url

    if mode == "ai":
        keyboard = [
            [
                InlineKeyboardButton("3", callback_data="count:3"),
                InlineKeyboardButton("5", callback_data="count:5"),
                InlineKeyboardButton("10", callback_data="count:10"),
            ],
            [InlineKeyboardButton("🤖 AI Decide", callback_data="count:ai")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
        ]

        await update.message.reply_text(
            "🎥 YouTube URL received!\n\nHow many clips do you want?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    context.user_data["waiting_for_timestamp"] = True
    await update.message.reply_text(
        "🎥 YouTube URL received!\n\n"
        "⏱️ Send the timestamp range.\n\n"
        "Example:\n01:20 - 01:45"
    )


async def ask_subtitles(query, context):
    keyboard = [
        [
            InlineKeyboardButton("🔤 English", callback_data="subs:english"),
            InlineKeyboardButton("🗣️ Native", callback_data="subs:native"),
            InlineKeyboardButton("🚫 None", callback_data="subs:none"),
        ],
        [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
    ]
    await query.edit_message_text(
        "Which subtitles?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def parse_timestamp_range(value: str):
    value = value.strip()

    if " - " in value:
        start, end = value.split(" - ", 1)
    elif "-" in value:
        start, end = value.split("-", 1)
    else:
        raise ValueError("Use format: 01:20 - 01:45")

    start = start.strip()
    end = end.strip()

    if time_to_seconds(end) <= time_to_seconds(start):
        raise ValueError("End time must be greater than start time.")

    return start, end


async def telegram_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await reject_if_unauthorized(update):
        return
    if context.user_data.get("waiting_for_timestamp"):
        try:
            start, end = parse_timestamp_range(update.message.text)
            context.user_data["timestamp"] = (start, end)
            context.user_data["waiting_for_timestamp"] = False

            await update.message.reply_text(
                f"⏱️ Timestamp received: {start} → {end}\n\n"
                "Which subtitles?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔤 English", callback_data="subs:english"),
                        InlineKeyboardButton("🗣️ Native", callback_data="subs:native"),
                        InlineKeyboardButton("🚫 None", callback_data="subs:none"),
                    ],
                    [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
                ]),
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Invalid timestamp.\n\n{e}\n\nExample: 01:20 - 01:45"
            )
        return

    await telegram_url(update, context)
