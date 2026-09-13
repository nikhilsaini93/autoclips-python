import logging
import time
import uuid
import asyncio
import shutil
import os
from pathlib import Path
from typing import List, Literal, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _parse_allowed_chats() -> set:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "") or ""
    allowed = {c.strip() for c in raw.split(",") if c.strip()}
    if TELEGRAM_CHAT_ID:
        allowed.add(str(TELEGRAM_CHAT_ID).strip())
    return allowed


ALLOWED_CHAT_IDS = _parse_allowed_chats()


def is_chat_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return str(chat_id) in ALLOWED_CHAT_IDS


async def _reject_if_unauthorized(update: Update) -> bool:
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


telegram_bot = None
_tg_request = None
_tg_updates_request = None
if TELEGRAM_BOT_TOKEN:
    # Video uploads need generous timeouts — the defaults (~5s write)
    # time out on slow links even for small clips.
    from telegram.request import HTTPXRequest

    _tg_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=180.0,
        pool_timeout=30.0,
    )
    # Polling (getUpdates long-poll) gets its own request object so its
    # read_timeout can stay comfortably above the long-poll `timeout`
    # below. A shared object is fine too, but separating them makes the
    # relationship explicit and avoids upload tuning breaking polling.
    _tg_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=30.0,
        read_timeout=65.0,
        write_timeout=60.0,
        pool_timeout=30.0,
    )
    telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN, request=_tg_request)


def _polling_error_callback(exc: TelegramError) -> None:
    """Error callback for Updater polling — must be sync (not async).

    Transient network blips (httpx.ReadError wrapped as NetworkError,
    TimedOut) are expected during long jobs / flaky links and are
    retried automatically by PTB's network_retry_loop. Log them as a
    one-line warning instead of the default full ERROR traceback so
    logs stay readable. Anything else keeps the full traceback.
    """
    if isinstance(exc, (NetworkError, TimedOut)):
        logging.getLogger("telegram.ext.Updater").warning(
            "Polling hiccup (%s: %s) — retrying automatically, jobs keep running.",
            exc.__class__.__name__,
            exc,
        )
        return
    logging.getLogger("telegram.ext.Updater").exception(
        "Exception happened while polling for updates.", exc_info=exc
    )

from logging_config import request_id_var, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from video_utils import (
    CLIPS_DIR,
    DOWNLOAD_DIR,
    TMP_DIR,
    download_video,
    find_viral_clips,
    get_video_credit,
    get_video_duration,
    get_video_id,
    render_video,
    time_to_seconds,
    transcribe_words_english,
    transcribe_words_native,
    words_to_transcript_text,
    write_srt,
)

# ---------------------------------------------------------------------------
# Telegram application
# ---------------------------------------------------------------------------
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file before starting the app."
    )

telegram_app = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .request(_tg_request)  # long write timeouts for send_video
    .get_updates_request(_tg_updates_request)  # read_timeout (65s) > polling timeout (30s)
    .build()
)


async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_unauthorized(update):
        return
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("ðŸŽ¬ Create Shorts", callback_data="create_shorts")],
        [InlineKeyboardButton("ðŸ—‘ï¸ Delete Files", callback_data="delete_menu")],
    ]

    await update.message.reply_text(
        "ðŸ¤– AutoClips Bot\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def telegram_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not ("youtube.com/" in url or "youtu.be/" in url):
        await update.message.reply_text("âŒ Please send a valid YouTube URL.")
        return

    mode = context.user_data.get("mode")

    # Prevent the old behavior: URL is accepted only after a mode is selected.
    if mode not in ("ai", "timestamp"):
        await update.message.reply_text(
            "Please choose an option first:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ðŸŽ¬ Create Shorts", callback_data="create_shorts")],
                [InlineKeyboardButton("ðŸ—‘ï¸ Delete Files", callback_data="delete_menu")],
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
            [InlineKeyboardButton("ðŸ¤– AI Decide", callback_data="count:ai")],
            [InlineKeyboardButton("âŒ CANCEL", callback_data="cancel")],
        ]

        await update.message.reply_text(
            "ðŸŽ¥ YouTube URL received!\n\nHow many clips do you want?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    context.user_data["waiting_for_timestamp"] = True
    await update.message.reply_text(
        "ðŸŽ¥ YouTube URL received!\n\n"
        "â±ï¸ Send the timestamp range.\n\n"
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


async def run_ai_generation(query, context):
    url = context.user_data.get("youtube_url")
    max_clips = context.user_data.get("max_clips")
    subtitles = context.user_data.get("subtitles", "native")

    if not url:
        await query.edit_message_text(
            "❌ YouTube URL not found. Please start again with /start."
        )
        return

    job_id = create_job("ai_viral")
    await query.edit_message_text(
        "⏳ Generating viral Shorts...\n\n"
        f"{_progress_bar(0)}\n"
        f"📥 Downloading video...\n"
        f"🎬 Clips: {'AI Decide' if max_clips is None else max_clips}\n"
        f"📝 Subtitles: {_subtitle_label(subtitles)}\n"
        f"🆔 Job: {job_id}\n\n"
        "This may take a while for long videos."
    )

    chat_id = query.message.chat_id if query.message else None
    message_id = query.message.message_id if query.message else None
    progress_task = None
    if chat_id is not None and message_id is not None:
        progress_task = asyncio.create_task(
            _run_progress_editor(
                context.bot,
                chat_id,
                message_id,
                job_id,
                lambda job: _format_ai_progress(job, max_clips, subtitles),
            )
        )

    try:
        req = ViralClipsRequest(
            url=url,
            max_clips=max_clips,
            vertical_crop=True,
            subtitles=subtitles,
        )
        # NOTE: generate_viral_clips is itself non-blocking towards the event
        # loop (all heavy work inside it runs via asyncio.to_thread), so it's
        # safe to await directly here without freezing the bot.
        update_job(job_id, progress="downloading/transcribing/rendering", stage="downloading", percent=2)
        video_id, entries = await generate_viral_clips(req, job_id=job_id)

        sent_count = sum(1 for e in entries if e.get("sent"))
        update_job(job_id, status="done", stage="done", percent=100,
                   done_clips=len(entries), result={"clips": len(entries), "sent": sent_count, "video_id": video_id})
        done_text = (
            f"✅ Done!\n\n🎬 Rendered {len(entries)} Shorts, sent {sent_count} to Telegram.\n🆔 Job: {job_id}"
        )
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=done_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=done_text)
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=done_text,
            )
    except Exception as e:
        logger.exception("Telegram AI generation failed")
        update_job(job_id, status="failed", stage="failed", error=str(e))
        fail_text = f"❌ Generation failed.\n\nError: {str(e)}\n🆔 Job: {job_id}"
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=fail_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=fail_text)
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=fail_text,
            )


async def run_timestamp_generation(query, context):
    url = context.user_data.get("youtube_url")
    timestamp = context.user_data.get("timestamp")
    subtitles = context.user_data.get("subtitles", "native")

    if not url or not timestamp:
        await query.edit_message_text(
            "âŒ Missing URL or timestamp. Please start again with /start."
        )
        return

    start, end = timestamp

    job_id = create_job("timestamp")
    await query.edit_message_text(
        "⏳ Creating timestamp clip...\n\n"
        f"{_progress_bar(0)}\n"
        f"📥 Downloading video...\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {_subtitle_label(subtitles)}\n"
        f"🆔 Job: {job_id}"
    )

    chat_id = query.message.chat_id if query.message else None
    message_id = query.message.message_id if query.message else None
    progress_task = None
    if chat_id is not None and message_id is not None:
        update_job(job_id, stage="downloading", percent=2)
        progress_task = asyncio.create_task(
            _run_progress_editor(
                context.bot,
                chat_id,
                message_id,
                job_id,
                lambda job: _format_timestamp_progress(job, start, end, subtitles),
            )
        )

    try:
        # All blocking (CPU/network/disk) calls are pushed to a worker thread
        # so the bot's event loop stays free to handle other updates while
        # this clip is being downloaded/transcribed/rendered.
        update_job(job_id, progress="downloading", stage="downloading", percent=5)
        video_id, video_path = await asyncio.to_thread(_prepare, url)

        update_job(job_id, progress="transcribing" if subtitles != "none" else "rendering",
                   stage="transcribing" if subtitles != "none" else "rendering",
                   percent=40 if subtitles != "none" else 60)
        words = (
            await asyncio.to_thread(_resolve_subtitle_words, video_id, video_path, subtitles)
            if subtitles != "none"
            else None
        )

        output_path = (
            CLIPS_DIR / f"{video_id}-timestamp-{uuid.uuid4().hex[:8]}.mp4"
        )

        update_job(job_id, progress="rendering", stage="rendering", percent=75)
        await asyncio.to_thread(
            render_video,
            video_path,
            output_path,
            time_to_seconds(start),
            time_to_seconds(end),
            words=words,
            vertical_crop=True,
        )

        update_job(job_id, progress="sending", stage="sending", percent=92)
        sent = await send_clip_to_telegram(
            video_path=output_path,
            clip_id=f"{video_id}-timestamp",
            title="Timestamp Clip",
            score=None,
            reason=f"{start} → {end}",
        )

        update_job(job_id, status="done", stage="done", percent=100, result={"file": output_path.name, "sent": sent})
        done_text = (
            f"✅ Timestamp clip generated and sent.\n🆔 Job: {job_id}"
            if sent else
            f"⚠️ Timestamp clip rendered but Telegram delivery failed.\nFile: clips/{output_path.name}\n🆔 Job: {job_id}"
        )
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=done_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=done_text)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=done_text)
    except Exception as e:
        logger.exception("Telegram timestamp generation failed")
        update_job(job_id, status="failed", stage="failed", error=str(e))
        fail_text = f"❌ Timestamp generation failed.\n\nError: {str(e)}\n🆔 Job: {job_id}"
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=fail_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=fail_text)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=fail_text)


async def telegram_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_unauthorized(update):
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
                f"âŒ Invalid timestamp.\n\n{e}\n\nExample: 01:20 - 01:45"
            )
        return

    await telegram_url(update, context)


def _delete_all_files_sync():
    """Blocking filesystem cleanup — run via asyncio.to_thread."""
    deleted_count = 0
    freed_bytes = 0

    folders = [DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR]

    videos_dir = Path(__file__).resolve().parent / "videos"
    if videos_dir.exists():
        folders.append(videos_dir)

    for folder in folders:
        if not folder.exists():
            continue

        for item in list(folder.iterdir()):
            try:
                if item.is_file() or item.is_symlink():
                    if item.is_file():
                        freed_bytes += item.stat().st_size
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    for p in item.rglob("*"):
                        if p.is_file():
                            try:
                                freed_bytes += p.stat().st_size
                            except OSError:
                                pass
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception:
                logger.exception("Failed to delete %s", item)

    return deleted_count, freed_bytes


async def delete_all_files():
    return await asyncio.to_thread(_delete_all_files_sync)


async def telegram_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_unauthorized(update):
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
                "âŒ Cancelled.\n\nSend /start to open the menu."
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram cancel edit failed: %s", exc)
        return

    if data == "create_shorts":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("ðŸ¤– AI Viral Clips", callback_data="mode_ai")],
            [InlineKeyboardButton("â±ï¸ Timestamp Clip", callback_data="mode_timestamp")],
            [InlineKeyboardButton("âŒ CANCEL", callback_data="cancel")],
        ]
        try:
            await query.edit_message_text(
                "ðŸŽ¬ Create Shorts\n\nChoose the clip type:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram create_shorts edit failed: %s", exc)
        return

    if data == "delete_menu":
        keyboard = [[
            InlineKeyboardButton("ðŸ—‘ï¸ DELETE ALL", callback_data="delete_confirm"),
            InlineKeyboardButton("âŒ CANCEL", callback_data="cancel"),
        ]]
        try:
            await query.edit_message_text(
                "âš ï¸ Delete all saved files?\n\n"
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
            await query.edit_message_text("ðŸ—‘ï¸ Deleting files...")
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram delete_confirm edit failed: %s", exc)
        try:
            count, freed = await delete_all_files()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "âœ… Cleanup complete!\n\n"
                    f"ðŸ—‘ï¸ Items deleted: {count}\n"
                    f"ðŸ’¾ Space freed: {freed / (1024 * 1024):.2f} MB"
                ),
            )
        except Exception as e:
            logger.exception("Telegram cleanup failed")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"âŒ Cleanup failed.\n\nError: {str(e)}",
            )
        return

    if data == "mode_ai":
        context.user_data["mode"] = "ai"
        try:
            await query.edit_message_text(
                "ðŸ¤– AI Viral Clips\n\nNow send the YouTube video URL."
            )
        except TelegramError as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Telegram mode_ai edit failed: %s", exc)
        return

    if data == "mode_timestamp":
        context.user_data["mode"] = "timestamp"
        try:
            await query.edit_message_text(
                "â±ï¸ Timestamp Clip\n\nNow send the YouTube video URL."
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

    if data.startswith("approve:"):
        await _handle_approve(update, context, data.split(":", 1)[1])
        return

    if data.startswith("deny:"):
        await _handle_deny(update, context, data.split(":", 1)[1])
        return


async def _handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Uploads the approved clip to YouTube as Unlisted (single-use token)."""
    query = update.callback_query
    chat_id = query.message.chat_id if query.message else None
    entry = pop_pending_upload(token)
    if entry is None:
        await query.answer("⏳ Approval expired (bot restarted?). Clip file is untouched.", show_alert=True)
        return
    await query.answer("Uploading to YouTube as Unlisted…")
    job_id = create_job("yt_upload")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Uploading {entry['clip_id']} to YouTube as Unlisted…\n🆔 Job: {job_id}",
        )
    except TelegramError:
        pass
    try:
        from youtube_upload import upload_unlisted

        result = await asyncio.to_thread(
            upload_unlisted,
            entry["path"],
            entry["title"],
            entry.get("description"),
            entry.get("hashtags"),
            None,
            entry.get("source_video_id") or None,
            None,
            entry.get("credit") or None,
        )
        update_job(job_id, status="done", result=result)
        try:
            await query.edit_message_caption(
                caption=f"✅ Uploaded as Unlisted\n\n🎯 {entry['title']}\n🔗 {result.get('url', '')}\n🆔 {entry['clip_id']}"
            )
        except TelegramError:
            logger.debug("Could not edit caption for %s, sending message instead", entry["clip_id"])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Uploaded as Unlisted\n\n🎯 {entry['title']}\n🔗 {result.get('url', '')}\n🆔 {entry['clip_id']}",
            )
        logger.info("Approved upload done clip=%s yt=%s", entry["clip_id"], result.get("video_id"))
    except Exception as e:
        logger.exception("YouTube upload failed for %s", entry["clip_id"])
        update_job(job_id, status="failed", error=str(e))
        # Re-register so the user can retry Approve (file is kept).
        retry_token = register_pending_upload(
            entry["path"], entry["clip_id"], entry["title"],
            entry.get("description"), entry.get("hashtags"), chat_id,
            source_video_id=entry.get("source_video_id"), credit=entry.get("credit"),
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Upload failed for {entry['clip_id']}.\n\nError: {str(e)}\n"
                    "The clip file was kept — tap Approve again to retry."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Retry upload", callback_data=f"approve:{retry_token}"),
                    InlineKeyboardButton("❌ Deny (delete)", callback_data=f"deny:{retry_token}"),
                ]]),
            )
        except TelegramError:
            pass


async def _handle_deny(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Deletes the denied clip MP4 to free space (single-use token)."""
    query = update.callback_query
    entry = pop_pending_upload(token)
    if entry is None:
        await query.answer("Already processed.", show_alert=True)
        return
    await query.answer("Deleting clip…")
    freed_mb = 0.0
    try:
        p = Path(entry["path"])
        if p.is_file():
            freed_mb = p.stat().st_size / (1024 * 1024)
            p.unlink()
            logger.info("Denied clip deleted: %s (%.1f MB)", entry["clip_id"], freed_mb)
            text = f"🗑️ Deleted {entry['clip_id']}\n💾 Freed {freed_mb:.1f} MB"
        else:
            text = f"🗑️ {entry['clip_id']} was already gone."
    except OSError as e:
        logger.exception("Deny-delete failed for %s", entry["clip_id"])
        text = f"❌ Delete failed for {entry['clip_id']}.\nError: {e}"
    try:
        await query.edit_message_caption(caption=text)
    except TelegramError:
        try:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text)
        except TelegramError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app.add_handler(CommandHandler("start", telegram_start))
    telegram_app.add_handler(CallbackQueryHandler(telegram_button))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text_router)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    # timeout=30 (long-poll hold) must stay below get_updates read_timeout
    # (65s). poll_interval avoids hammering on fast failures, bootstrap
    # retries cover startup blips, and the custom error_callback downgrades
    # transient NetworkError/TimedOut to a warning (auto-retried anyway).
    await telegram_app.updater.start_polling(
        poll_interval=2.0,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
        error_callback=_polling_error_callback,
    )

    logger.info("Telegram bot polling started")

    if not (os.getenv("YT_CLIENT_ID") and os.getenv("YT_CLIENT_SECRET") and os.getenv("YT_REFRESH_TOKEN")):
        logger.warning(
            "YouTube upload NOT configured (YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN missing) — "
            "Telegram Approve will fail until you run get_youtube_token.py (see YOUTUBE_SETUP.md)."
        )

    yield

    logger.info("Stopping Telegram bot")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)

# Rate limiting (no auth per scope — expensive routes get per-IP caps).
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda request, exc: JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"}),
)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Tags every log line produced while handling this request with a short
    request id (visible in console + logs/app.log), and logs start/end/timing."""
    req_id = uuid.uuid4().hex[:8]
    token = request_id_var.set(req_id)
    t0 = time.perf_counter()
    logger.info("--> %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("!! %s %s failed after %.1fs", request.method, request.url.path, time.perf_counter() - t0)
        request_id_var.reset(token)
        raise
    elapsed = time.perf_counter() - t0
    logger.info("<-- %s %s %d in %.1fs", request.method, request.url.path, response.status_code, elapsed)
    response.headers["X-Request-ID"] = req_id
    request_id_var.reset(token)
    return response


class SubtitleRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    burn_in: bool = Field(
        False, description="If true, returns an MP4 with subtitles burned in. If false, returns a .srt file."
    )
    vertical_crop: bool = Field(
        False, description="Only used when burn_in=true: crop the video to 9:16 vertical (face-aware)."
    )


class ClipRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    start: str = Field(..., description="Start time — 'HH:MM:SS', 'MM:SS', or seconds")
    end: str = Field(..., description="End time — 'HH:MM:SS', 'MM:SS', or seconds")
    vertical_crop: bool = Field(True, description="Crop to 9:16 vertical using face detection")
    subtitles: Literal["none", "english", "native"] = Field(
        "none", description="Burn subtitles into the clip: none, english (translated), or native (as spoken)"
    )


class AnalyzeClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to let the AI decide the count entirely on its own."
    )


class ViralClipInfo(BaseModel):
    title: Optional[str] = None
    start: str
    end: str
    duration_seconds: float
    score: Optional[float] = None
    reason: Optional[str] = None
    hashtags: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class ViralClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to get every viral-worthy clip Gemini finds (typically 6-10)."
    )
    vertical_crop: bool = Field(True, description="Crop each clip to 9:16 vertical using face detection")
    subtitles: Literal["none", "english", "native"] = Field(
        "english", description="Burn subtitles into each clip: none, english (translated), or native (as spoken)"
    )


def _prepare(url: str):
    try:
        video_id = get_video_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    video_path = download_video(video_id)
    return video_id, video_path


def _subtitle_label(value: str) -> str:
    return {"english": "English", "native": "Native", "none": "NO"}.get(value, value)


def _resolve_subtitle_words(video_id: str, video_path, subtitles: str):
    """Returns word list for burning, or None. 'native' reuses the
    as-spoken transcript (timestamps align exactly with clip boundaries);
    'english' uses Whisper's translate task."""
    if subtitles == "english":
        return transcribe_words_english(video_id, video_path)
    if subtitles == "native":
        return transcribe_words_native(video_id, video_path)["words"]
    return None


# In-memory job tracker for BackgroundTasks flows (no Redis per scope).
# Telegram long generations create entries here; clients poll GET /jobs/{id}.
jobs: dict = {}


def create_job(kind: str) -> str:
    now = time.time()
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "progress": "",
        "stage": "",
        "percent": 0,
        "total_clips": 0,
        "done_clips": 0,
        "detail": "",
        "result": None,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    return job_id


def update_job(job_id: str, **fields):
    if job_id in jobs:
        fields.setdefault("updated_at", time.time())
        jobs[job_id].update(fields)


def _progress_bar(percent: int, width: int = 10) -> str:
    """Renders e.g. '██████░░░░ 62%' for Telegram status messages."""
    try:
        pct = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        pct = 0
    filled = round(pct / 100 * width)
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


def _format_elapsed(job: dict) -> str:
    try:
        elapsed = int(time.time() - float(job.get("started_at", time.time())))
    except (TypeError, ValueError):
        elapsed = 0
    return f"{elapsed // 60:02d}:{elapsed % 60:02d}"


_STAGE_LABELS = {
    "downloading": "📥 Downloading video...",
    "transcribing": "📝 Transcribing speech (long videos take a while on CPU)...",
    "analyzing": "🤖 AI finding viral moments...",
    "rendering": "🎬 Rendering clips...",
    "sending": "📤 Sending to Telegram...",
    "done": "✅ Done",
    "failed": "❌ Failed",
}


def _format_ai_progress(job: dict, max_clips, subtitles: str) -> str:
    percent = int(job.get("percent", 0) or 0)
    stage = job.get("stage", "") or job.get("progress", "")
    total = int(job.get("total_clips", 0) or 0)
    done = int(job.get("done_clips", 0) or 0)
    label = _STAGE_LABELS.get(stage, stage or "Working...")
    # Show clip counter once Gemini count is known.
    if total > 0 and stage in ("rendering", "sending"):
        counter = f"\n🎬 Clips: {done}/{total} done"
    else:
        counter = f"\n🎬 Clips: {'AI Decide' if max_clips is None else max_clips}"
    return (
        "⏳ Generating viral Shorts...\n\n"
        f"{_progress_bar(percent)}\n"
        f"{label}{counter}\n"
        f"📝 Subtitles: {_subtitle_label(subtitles)}\n"
        f"⏱️ Elapsed: {_format_elapsed(job)}\n"
        f"🆔 Job: {job.get('job_id', '')}"
    )


def _format_timestamp_progress(job: dict, start: str, end: str, subtitles: str) -> str:
    percent = int(job.get("percent", 0) or 0)
    stage = job.get("stage", "") or job.get("progress", "")
    label = _STAGE_LABELS.get(stage, stage or "Working...")
    return (
        "⏳ Creating timestamp clip...\n\n"
        f"{_progress_bar(percent)}\n"
        f"{label}\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {_subtitle_label(subtitles)}\n"
        f"⏱️ Elapsed: {_format_elapsed(job)}\n"
        f"🆔 Job: {job.get('job_id', '')}"
    )


async def _run_progress_editor(bot, chat_id, message_id, job_id: str, build_text, interval: float = 15.0):
    """Edits the SAME Telegram status message with live % until job finishes.

    Throttled to one edit per `interval` seconds to stay well under Telegram
    rate limits. Never raises — progress must not break the real job.
    """
    last_text = ""
    try:
        while True:
            job = jobs.get(job_id)
            if not job:
                return
            if job.get("status") != "running":
                return
            try:
                text = build_text(job)
            except Exception:
                logger.exception("Progress formatter failed for job %s", job_id)
                return
            if text != last_text:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                    last_text = text
                except RetryAfter as e:
                    wait = min(getattr(e, "retry_after", 15) or 15, 60)
                    logger.debug("Progress edit rate-limited for job %s, waiting %ss", job_id, wait)
                    await asyncio.sleep(wait)
                    continue
                except (TimedOut, TelegramError):
                    # MessageNotModified / deleted message / transient network —
                    # keep the job running, just skip this tick.
                    logger.debug("Progress edit skipped for job %s", job_id, exc_info=True)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Progress editor crashed for job %s", job_id)


# Pending YouTube-upload approvals (per-clip Approve/Deny buttons).
# token -> {"path": str, "clip_id": str, "title": str,
#           "description": str, "hashtags": list, "chat_id": str,
#           "source_video_id": str, "credit": str}
# In-memory like `jobs`: a restart expires pending approvals.
pending_uploads: dict = {}


def register_pending_upload(path, clip_id, title, description, hashtags, chat_id, source_video_id=None, credit=None) -> str:
    token = uuid.uuid4().hex[:8]
    pending_uploads[token] = {
        "path": str(path),
        "clip_id": clip_id,
        "title": title or "Untitled Clip",
        "description": description or "",
        "hashtags": list(hashtags or []),
        "chat_id": str(chat_id) if chat_id is not None else "",
        "source_video_id": source_video_id or "",
        "credit": credit or "",
    }
    return token


def pop_pending_upload(token: str):
    return pending_uploads.pop(token, None)


@app.get("/health")
def health():
    import shutil as _shutil

    try:
        import subprocess as _sp

        _sp.run(["ffmpeg", "-version"], capture_output=True, check=True)
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False
    try:
        import subprocess as _sp2

        _sp2.run(["ffprobe", "-version"], capture_output=True, check=True)
        ffprobe_ok = True
    except Exception:
        ffprobe_ok = False
    try:
        total, used, free = _shutil.disk_usage(".")
        disk_free_mb = round(free / (1024 * 1024), 1)
    except Exception:
        disk_free_mb = None
    return {
        "status": "ok",
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "disk_free_mb": disk_free_mb,
        "whisper_model": os.getenv("WHISPER_MODEL_SIZE", "base"),
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/admin/cleanup")
@limiter.limit("10/minute")
def admin_cleanup(request: Request, max_age_hours: float = 72):
    """Delete files older than max_age_hours in downloads/clips/tmp."""
    from video_utils import cleanup_old_files

    result = cleanup_old_files(max_age_hours=max_age_hours)
    return {"success": True, **result}


@app.post("/subtitles/english")
@limiter.limit("20/minute")
def subtitles_english(request: Request, req: SubtitleRequest):
    """Transcribes the video and translates it to English (works even if the
    source audio is Hindi/Urdu/etc, via Whisper's translate task)."""
    logger.info("english subtitles requested url=%s burn_in=%s vertical_crop=%s", req.url, req.burn_in, req.vertical_crop)
    video_id, video_path = _prepare(req.url)
    words = transcribe_words_english(video_id, video_path)
    duration = get_video_duration(video_path)

    if not req.burn_in:
        srt_path = TMP_DIR / f"{video_id}-english.srt"
        if not write_srt(words, 0.0, duration, srt_path):
            raise HTTPException(422, "No speech detected")
        logger.info("Returning english srt for video_id=%s", video_id)
        return FileResponse(srt_path, filename=f"{video_id}-english.srt", media_type="application/x-subrip")

    output_path = CLIPS_DIR / f"{video_id}-english-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, 0.0, duration, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning english mp4 for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


@app.post("/clip")
@limiter.limit("20/minute")
def clip(request: Request, req: ClipRequest):
    """Cuts [start, end] out of the video, optionally with a 9:16 face-aware
    crop and/or burned-in English subtitles."""
    logger.info(
        "clip requested url=%s start=%s end=%s vertical_crop=%s subtitles=%s",
        req.url, req.start, req.end, req.vertical_crop, req.subtitles,
    )
    video_id, video_path = _prepare(req.url)
    start_sec = time_to_seconds(req.start)
    end_sec = time_to_seconds(req.end)
    if end_sec <= start_sec:
        raise HTTPException(400, "end must be after start")

    words = _resolve_subtitle_words(video_id, video_path, req.subtitles)

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


def _analyze_viral_clips(video_id: str, video_path, max_clips: Optional[int]) -> list:
    """Shared by /clips/analyze and /clips/viral: builds a native-language
    transcript and asks Gemini for Hinglish titles/descriptions, then wraps
    each description with Credit + Original link + fair-use disclaimer."""
    native = transcribe_words_native(video_id, video_path)
    detected_language = native.get("language")
    transcript_text = words_to_transcript_text(native["words"])
    logger.info("clip analysis language=%s video_id=%s", detected_language, video_id)
    try:
        candidates = find_viral_clips(transcript_text, max_clips=max_clips, language=detected_language)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not candidates:
        raise HTTPException(422, "Gemini did not find any viral-worthy clips in this video")
    try:
        from youtube_upload import build_yt_description
        credit = get_video_credit(video_id) or ""
        for c in candidates:
            c["description"] = build_yt_description(
                c.get("description"), video_id=video_id, credit=credit,
            )
            c["_credit"] = credit
            c["_source_video_id"] = video_id
    except Exception:
        logger.exception("Description footer enrichment failed for video_id=%s", video_id)
    return candidates


@app.post("/clips/analyze", response_model=List[ViralClipInfo])
@limiter.limit("10/minute")
def clips_analyze(request: Request, req: AnalyzeClipsRequest):
    """AI-only analysis pass: no rendering, no video files produced. The
    model watches the transcript, decides for itself how many clips are
    worth cutting (anywhere from 1 to 10+), and returns each one's
    timestamps, a title, a confidence score, and its reasoning - so you can
    review the picks before spending time actually rendering any of them."""
    logger.info("clip analysis requested url=%s max_clips=%s", req.url, req.max_clips)
    video_id, video_path = _prepare(req.url)
    candidates = _analyze_viral_clips(video_id, video_path, req.max_clips)

    results = []
    for i, c in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(c["start"])
            end_sec = time_to_seconds(c["end"])
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed candidate #%d (%s): %s", i, c, e)
            continue
        if end_sec <= start_sec:
            logger.warning("Skipping candidate #%d with end<=start: %s", i, c)
            continue
        results.append(ViralClipInfo(
            title=c.get("title"),
            start=c["start"],
            end=c["end"],
            duration_seconds=round(end_sec - start_sec, 1),
            score=c.get("score"),
            reason=c.get("reason"),
            hashtags=c.get("hashtags") or [],
            description=c.get("description"),
        ))

    if not results:
        raise HTTPException(422, "All candidate clips were malformed")

    logger.info("Analysis found %d viral-worthy clip(s) for video_id=%s", len(results), video_id)
    return results


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
    if not telegram_bot:
        logger.warning("Telegram bot is not configured")
        return False

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
                chat_id=TELEGRAM_CHAT_ID,
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
            TELEGRAM_CHAT_ID, source_video_id=source_video_id, credit=credit,
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
                    chat_id=TELEGRAM_CHAT_ID,
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
                chat_id=TELEGRAM_CHAT_ID,
                text=f"❌ Could not deliver {clip_id} to Telegram (network timeout).\n"
                     f"File kept at: clips/{Path(video_path).name}",
            )
        except TelegramError:
            pass
        return False

    logger.info("Sent clip %s to Telegram", clip_id)
    if overflow:
        try:
            await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"📄 Full YT pack for {clip_id}:\n\n{overflow}")
        except TelegramError:
            logger.exception("Failed to send overflow pack for %s", clip_id)
    return True


async def generate_viral_clips(req: ViralClipsRequest, job_id: str | None = None):
    """Generate viral clips and send each generated MP4 to Telegram.

    Returns (video_id, entries). All blocking work (download, transcription,
    Gemini analysis, rendering) is pushed to worker threads via
    asyncio.to_thread so this coroutine never blocks the event loop — this
    matters because the same loop also runs the Telegram bot's polling.
    """

    def _progress(msg: str, percent: int | None = None, **extra):
        if job_id:
            fields = {"progress": msg}
            # Keep machine-readable stage in sync when caller uses a known name.
            if msg.split(" ")[0] in ("downloading", "transcribing", "analyzing", "rendering", "sending"):
                fields["stage"] = msg.split(" ")[0]
            if percent is not None:
                fields["percent"] = max(0, min(100, int(percent)))
            fields.update(extra)
            update_job(job_id, **fields)

    logger.info(
        "viral clips requested url=%s max_clips=%s vertical_crop=%s subtitles=%s",
        req.url,
        req.max_clips,
        req.vertical_crop,
        req.subtitles,
    )

    _progress("downloading", percent=5, stage="downloading")
    video_id, video_path = await asyncio.to_thread(_prepare, req.url)

    _progress("transcribing", percent=15, stage="transcribing",
              detail="native transcript for AI analysis (slowest step on CPU)")
    _progress("analyzing", percent=20, stage="analyzing")
    candidates = await asyncio.to_thread(
        _analyze_viral_clips,
        video_id,
        video_path,
        req.max_clips,
    )

    # Gemini count is now known — clip-phase % can be computed honestly.
    total = len(candidates)
    if job_id:
        update_job(job_id, total_clips=total, done_clips=0)

    _progress("transcribing" if req.subtitles != "none" else "rendering",
              percent=68 if req.subtitles != "none" else 72,
              stage="transcribing" if req.subtitles != "none" else "rendering")
    subtitle_words = (
        await asyncio.to_thread(_resolve_subtitle_words, video_id, video_path, req.subtitles)
        if req.subtitles != "none"
        else None
    )

    entries = []

    for i, candidate in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(candidate["start"])
            end_sec = time_to_seconds(candidate["end"])
        except (KeyError, ValueError) as e:
            logger.warning(
                "Skipping malformed candidate #%d (%s): %s",
                i,
                candidate,
                e,
            )
            continue

        if end_sec <= start_sec:
            logger.warning(
                "Skipping candidate #%d with end<=start: %s",
                i,
                candidate,
            )
            continue

        logger.info(
            "Rendering viral clip %d/%d: '%s' [%s -> %s] score=%s",
            i,
            len(candidates),
            candidate.get("title"),
            candidate.get("start"),
            candidate.get("end"),
            candidate.get("score"),
        )
        _progress(f"rendering {i}/{len(candidates)}",
                    percent=75 + int(25 * (i - 1) / max(1, len(candidates))),
                    stage="rendering")

        output_path = (
            CLIPS_DIR
            / f"{video_id}-viral-{i}-{uuid.uuid4().hex[:8]}.mp4"
        )

        await asyncio.to_thread(
            render_video,
            video_path,
            output_path,
            start_sec,
            end_sec,
            words=subtitle_words,
            vertical_crop=req.vertical_crop,
        )

        entries.append({
            "path": output_path,
            "title": candidate.get("title"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "score": candidate.get("score"),
            "reason": candidate.get("reason"),
            "hashtags": candidate.get("hashtags") or [],
            "description": candidate.get("description"),
            "source_video_id": candidate.get("_source_video_id") or video_id,
            "credit": candidate.get("_credit") or "",
        })

        # Send immediately — user gets each clip as soon as it's rendered
        # instead of waiting for the whole batch. A failed send never stops
        # the remaining clips.
        _progress(f"sending {i}/{len(candidates)}",
                    percent=75 + int(25 * (i - 0.5) / max(1, len(candidates))),
                    stage="sending")
        try:
            entries[-1]["sent"] = await send_clip_to_telegram(
                video_path=output_path,
                clip_id=f"{video_id}-{i}",
                title=candidate.get("title"),
                score=candidate.get("score"),
                reason=candidate.get("reason"),
                hashtags=candidate.get("hashtags"),
                description=candidate.get("description"),
                source_video_id=candidate.get("_source_video_id") or video_id,
                credit=candidate.get("_credit") or "",
            )
        except Exception:
            logger.exception("Send failed for clip %s-%d, continuing batch", video_id, i)
            entries[-1]["sent"] = False
        if job_id:
            update_job(job_id, done_clips=len(entries),
                       percent=75 + int(25 * len(entries) / max(1, len(candidates))))

    if not entries:
        raise HTTPException(
            422,
            "All candidate clips were malformed - nothing to render",
        )

    sent_count = sum(1 for e in entries if e.get("sent"))
    logger.info(
        "Rendered %d clip(s), sent %d to Telegram for video_id=%s",
        len(entries),
        sent_count,
        video_id,
    )

    return video_id, entries


@app.post("/clips/viral")
@limiter.limit("5/minute")
async def clips_viral(request: Request, req: ViralClipsRequest):
    """Auto-find viral clips, render them, send them to Telegram, and return metadata."""

    video_id, entries = await generate_viral_clips(req)

    return {
        "success": True,
        "clips_sent": sum(1 for entry in entries if entry.get("sent")),
        "clips": [
            {
                "clip_id": f"{video_id}-{i}",
                "file": entry["path"].name,
                "title": entry["title"],
                "score": entry["score"],
                "reason": entry["reason"],
                "hashtags": entry.get("hashtags") or [],
                "description": entry.get("description"),
                "sent": bool(entry.get("sent")),
            }
            for i, entry in enumerate(entries, start=1)
        ],
    }
