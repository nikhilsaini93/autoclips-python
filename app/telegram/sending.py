import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError, TimedOut

from app.config import settings
from app.services.approvals import pending_uploads, register_pending_upload

logger = logging.getLogger(__name__)


async def send_clip_to_telegram(
    video_path,
    clip_id,
    title,
    score,
    reason,
    hashtags=None,
    description=None,
    source_video_id=None,
    credit=None,
):
    """Sends one clip with Approve/Deny buttons.

    Returns True if the clip reached Telegram, False otherwise (in which
    case the approval token is dropped and the user is told the file path).
    """
    from app.telegram.bot import get_bot

    telegram_bot = get_bot()
    if not telegram_bot:
        logger.warning("Telegram bot is not configured")
        return False

    chat_id = settings.TELEGRAM_CHAT_ID

    # Telegram Bot API videos are capped at ~50MB — skip gracefully instead
    # of failing the whole batch when one clip is too large.
    try:
        size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    # Oversize path also returns False — no buttons were attached.
    if size_mb > 50:
        logger.warning("Skipping Telegram send for %s (%.1f MB > 50MB limit)", clip_id, size_mb)
        try:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Clip {clip_id} too large for Telegram ({size_mb:.1f} MB > 50MB).\n"
                     f"File kept at: {Path(video_path).name} (no Approve buttons — upload it manually).",
            )
        except TelegramError:
            logger.exception("Failed to send too-large notice for %s", clip_id)
        return False

    tags = [str(t).strip("# ").strip() for t in (hashtags or []) if str(t).strip("# ").strip()]
    hashtag_line = " ".join(f"#{t.replace(' ', '')}" for t in tags[:6])

    caption = (
        f"🎬 New Short Generated\n\n"
        f"🆔 Clip: {clip_id}\n"
        f"🔥 Viral Score: {score}\n\n"
        f"🎯 YT Title:\n{title or 'No title'}\n"
    )
    if hashtag_line:
        caption += f"\n{hashtag_line}\n"
    if description:
        caption += f"\n📄 Description:\n{description.strip()}\n"
    caption += f"\n💡 Reason:\n{reason or 'N/A'}"

    # Telegram video captions cap at 1024 chars — overflow goes as a reply.
    overflow = None
    if len(caption) > 1024:
        overflow = caption
        caption = caption[:1000] + "\n…(cont.)"

    # Per-clip Approve/Deny → YouTube Unlisted upload or delete.
    # Registered before sending so the buttons always resolve.
    try:
        approve_token = register_pending_upload(
            video_path, clip_id, title, description, tags,
            chat_id, source_video_id=source_video_id, credit=credit,
        )
    except Exception:
        logger.exception("Failed to register pending upload for %s", clip_id)
        approve_token = None
    keyboard = None
    if approve_token:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve → YouTube (Unlisted)", callback_data=f"approve:{approve_token}"),
            InlineKeyboardButton("❌ Deny (delete)", callback_data=f"deny:{approve_token}"),
        ]])

    sent = False
    for attempt in (1, 2, 3):
        try:
            with open(video_path, "rb") as video:
                await telegram_bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=caption,
                    reply_markup=keyboard,
                )
            sent = True
            break
        except RetryAfter as e:
            wait = min(getattr(e, "retry_after", 30) or 30, 90)
            logger.warning("Telegram rate-limited, waiting %ss (clip %s attempt %d/3)", wait, clip_id, attempt)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("Telegram send timed out for %s (attempt %d/3)", clip_id, attempt)
            await asyncio.sleep(5)
        except TelegramError:
            logger.exception("Failed to send clip %s to Telegram", clip_id)
            break

    if not sent:
        # User never got the buttons — drop the token so Approve can't
        # point at an undelivered clip, and tell them where the file is.
        if approve_token:
            pending_uploads.pop(approve_token, None)
        try:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=f"❌ Could not deliver {clip_id} to Telegram (network timeout).\n"
                     f"File kept at: clips/{Path(video_path).name}",
            )
        except TelegramError:
            pass
        return False

    logger.info("Sent clip %s to Telegram", clip_id)
    if overflow:
        try:
            await telegram_bot.send_message(chat_id=chat_id, text=f"📄 Full YT pack for {clip_id}:\n\n{overflow}")
        except TelegramError:
            logger.exception("Failed to send overflow pack for %s", clip_id)
    return True
