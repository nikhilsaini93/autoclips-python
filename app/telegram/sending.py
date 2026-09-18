import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

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

    # Generate metadata and save to text file alongside the video
    tags = [str(t).strip("# ").strip() for t in (hashtags or []) if str(t).strip("# ").strip()]
    hashtag_line = " ".join(f"#{t.replace(' ', '')}" for t in tags[:6])

    caption_full = (
        f"🎬 New Short Generated\n\n"
        f"🆔 Clip: {clip_id}\n"
        f"🔥 Viral Score: {score}\n\n"
        f"🎯 YT Title:\n{title or 'No title'}\n"
    )
    if hashtag_line:
        caption_full += f"\n{hashtag_line}\n"
    if description:
        caption_full += f"\n📄 Description:\n{description.strip()}\n"
    caption_full += f"\n💡 Reason:\n{reason or 'N/A'}"

    try:
        text_path = Path(video_path).with_suffix('.txt')
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(caption_full)
    except Exception:
        logger.exception("Failed to write metadata file for %s", clip_id)

    # Telegram Bot API videos are capped (~50MB default, configurable) — try to
    # recompress first, then skip gracefully instead of failing the whole batch.
    try:
        size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    cap_mb = float(getattr(settings, "TELEGRAM_MAX_VIDEO_MB", 50.0) or 50.0)
    if size_mb > cap_mb:
        # Best-effort recompress before giving up (unit tests patch this to
        # False to exercise the skip path).
        try:
            from app.services.video.render import compress_for_telegram
            try:
                compress_for_telegram(Path(video_path))
            except Exception:
                logger.exception("Recompress attempt failed for %s", clip_id)
            try:
                size_mb = Path(video_path).stat().st_size / (1024 * 1024)
            except OSError:
                pass
        except Exception:
            logger.exception("Oversize check failed for %s", clip_id)
    # Oversize path also returns False — no buttons were attached.
    if size_mb > cap_mb:
        logger.warning("Skipping Telegram send for %s (%.1f MB > %.1fMB limit)", clip_id, size_mb, cap_mb)
        try:
            warning_text = (
                f"⚠️ Clip {clip_id} too large for Telegram ({size_mb:.1f} MB > {cap_mb:.1f} MB, "
                f"Telegram 50 MB limit).\n"
                f"File kept at: {Path(video_path).name} (no Approve buttons — upload it manually).\n"
                f"Text file saved at: {text_path.name}\n\n"
                f"📝 Clip Metadata:\n{caption_full}"
            )
            # Telegram text limit is 4096, truncate if necessary
            if len(warning_text) > 4000:
                warning_text = warning_text[:4000] + "\n...[truncated]"

            await telegram_bot.send_message(
                chat_id=chat_id,
                text=warning_text,
            )
        except TelegramError:
            logger.exception("Failed to send too-large notice for %s", clip_id)
        return False

    caption = caption_full
    # Telegram video captions cap at 1024 chars — overflow goes as a reply.
    overflow = None
    if len(caption) > 1024:
        overflow = caption
        caption = caption[:1000] + "\n…(cont.)"

    # Per-clip Approve/Deny → YouTube / Instagram / Facebook upload or delete.
    # One token per platform pointing at the same file so each destination
    # can be approved (and retried) independently. Registered before sending
    # so the buttons always resolve.
    tokens: dict[str, str] = {}
    try:
        tokens["youtube"] = register_pending_upload(
            video_path, clip_id, title, description, tags,
            chat_id, source_video_id=source_video_id, credit=credit,
            platform="youtube",
        )
        try:
            from app.services.meta.tokens import (
                is_facebook_configured,
                is_instagram_configured,
            )

            if is_instagram_configured():
                tokens["instagram"] = register_pending_upload(
                    video_path, clip_id, title, description, tags,
                    chat_id, source_video_id=source_video_id, credit=credit,
                    platform="instagram",
                )
            if is_facebook_configured():
                tokens["facebook"] = register_pending_upload(
                    video_path, clip_id, title, description, tags,
                    chat_id, source_video_id=source_video_id, credit=credit,
                    platform="facebook",
                )
        except Exception:
            logger.exception("Meta platform check failed for %s (YT-only buttons)", clip_id)
    except Exception:
        logger.exception("Failed to register pending upload for %s", clip_id)
        tokens = {}
    keyboard = None
    if tokens:
        rows = []
        if "youtube" in tokens:
            rows.append([
                InlineKeyboardButton("✅ YouTube (Unlisted)", callback_data=f"approve:{tokens['youtube']}"),
            ])
        meta_row = []
        if "instagram" in tokens:
            meta_row.append(
                InlineKeyboardButton("📸 IG Reel", callback_data=f"approve_ig:{tokens['instagram']}"),
            )
        if "facebook" in tokens:
            meta_row.append(
                InlineKeyboardButton("📘 FB Reel", callback_data=f"approve_fb:{tokens['facebook']}"),
            )
        if meta_row:
            rows.append(meta_row)
        # Deny deletes the file — reuse the first token so the handler can
        # purge its siblings (other platforms) for the same clip.
        deny_token = tokens.get("youtube") or next(iter(tokens.values()))
        rows.append([
            InlineKeyboardButton("❌ Deny (delete)", callback_data=f"deny:{deny_token}"),
        ])
        keyboard = InlineKeyboardMarkup(rows)

    sent = False
    too_large_api = False
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
        except NetworkError as e:
            # "Request Entity Too Large" is a size rejection, not a transient
            # network blip — don't retry pointlessly, tell the truth.
            if "too large" in str(e).lower() or "entity" in str(e).lower():
                logger.warning("Telegram rejected %s as too large: %s", clip_id, e)
                too_large_api = True
                break
            logger.exception("Telegram network error for clip %s", clip_id)
            break
        except TelegramError:
            logger.exception("Failed to send clip %s to Telegram", clip_id)
            break

    if not sent:
        # User never got the buttons — drop the tokens so Approve can't
        # point at an undelivered clip, and tell them where the file is.
        for tok in tokens.values():
            pending_uploads.pop(tok, None)
        try:
            if too_large_api:
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Clip {clip_id} rejected by Telegram as too large (Telegram 50 MB limit).\n"
                         f"File kept at: clips/{Path(video_path).name} — upload it manually.",
                )
            else:
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
