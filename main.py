import logging
import time
import uuid
import asyncio
import shutil
import os
from pathlib import Path
from typing import List, Literal, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
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

telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

from logging_config import request_id_var, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from video_utils import (
    CLIPS_DIR,
    TMP_DIR,
    download_video,
    find_viral_clips,
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

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()


async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            InlineKeyboardButton("📝 YES", callback_data="subs:english"),
            InlineKeyboardButton("🚫 NO", callback_data="subs:none"),
        ],
        [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
    ]
    await query.edit_message_text(
        "Do you want subtitles?",
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
    subtitles = context.user_data.get("subtitles", "english")

    if not url:
        await query.edit_message_text(
            "❌ YouTube URL not found. Please start again with /start."
        )
        return

    await query.edit_message_text(
        "⏳ Generating viral Shorts...\n\n"
        f"🎬 Clips: {'AI Decide' if max_clips is None else max_clips}\n"
        f"📝 Subtitles: {'YES' if subtitles == 'english' else 'NO'}\n\n"
        "This may take some time."
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
        video_id, entries = await generate_viral_clips(req)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Done!\n\n🎬 Generated {len(entries)} Shorts.",
        )
    except Exception as e:
        logger.exception("Telegram AI generation failed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Generation failed.\n\nError: {str(e)}",
        )


async def run_timestamp_generation(query, context):
    url = context.user_data.get("youtube_url")
    timestamp = context.user_data.get("timestamp")
    subtitles = context.user_data.get("subtitles", "english")

    if not url or not timestamp:
        await query.edit_message_text(
            "❌ Missing URL or timestamp. Please start again with /start."
        )
        return

    start, end = timestamp

    await query.edit_message_text(
        "⏳ Creating timestamp clip...\n\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {'YES' if subtitles == 'english' else 'NO'}"
    )

    try:
        # All blocking (CPU/network/disk) calls are pushed to a worker thread
        # so the bot's event loop stays free to handle other updates while
        # this clip is being downloaded/transcribed/rendered.
        video_id, video_path = await asyncio.to_thread(_prepare, url)

        words = (
            await asyncio.to_thread(transcribe_words_english, video_id, video_path)
            if subtitles == "english"
            else None
        )

        output_path = (
            CLIPS_DIR / f"{video_id}-timestamp-{uuid.uuid4().hex[:8]}.mp4"
        )

        await asyncio.to_thread(
            render_video,
            video_path,
            output_path,
            time_to_seconds(start),
            time_to_seconds(end),
            words=words,
            vertical_crop=True,
        )

        await send_clip_to_telegram(
            video_path=output_path,
            clip_id=f"{video_id}-timestamp",
            title="Timestamp Clip",
            score=None,
            reason=f"{start} → {end}",
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✅ Timestamp clip generated and sent.",
        )
    except Exception as e:
        logger.exception("Telegram timestamp generation failed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Timestamp generation failed.\n\nError: {str(e)}",
        )


async def telegram_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_timestamp"):
        try:
            start, end = parse_timestamp_range(update.message.text)
            context.user_data["timestamp"] = (start, end)
            context.user_data["waiting_for_timestamp"] = False

            await update.message.reply_text(
                f"⏱️ Timestamp received: {start} → {end}\n\n"
                "Do you want subtitles?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📝 YES", callback_data="subs:english"),
                        InlineKeyboardButton("🚫 NO", callback_data="subs:none"),
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


def _delete_all_files_sync():
    """Blocking filesystem cleanup — run via asyncio.to_thread."""
    deleted_count = 0
    freed_bytes = 0

    folders = [CLIPS_DIR, TMP_DIR]

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
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Cancelled.\n\nSend /start to open the menu."
        )
        return

    if data == "create_shorts":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("🤖 AI Viral Clips", callback_data="mode_ai")],
            [InlineKeyboardButton("⏱️ Timestamp Clip", callback_data="mode_timestamp")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
        ]
        await query.edit_message_text(
            "🎬 Create Shorts\n\nChoose the clip type:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "delete_menu":
        keyboard = [[
            InlineKeyboardButton("🗑️ DELETE ALL", callback_data="delete_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="cancel"),
        ]]
        await query.edit_message_text(
            "⚠️ Delete all saved files?\n\n"
            "This removes downloaded videos, generated Shorts, "
            "and temporary files. The folders remain.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "delete_confirm":
        await query.edit_message_text("🗑️ Deleting files...")
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
        await query.edit_message_text(
            "🤖 AI Viral Clips\n\nNow send the YouTube video URL."
        )
        return

    if data == "mode_timestamp":
        context.user_data["mode"] = "timestamp"
        await query.edit_message_text(
            "⏱️ Timestamp Clip\n\nNow send the YouTube video URL."
        )
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app.add_handler(CommandHandler("start", telegram_start))
    telegram_app.add_handler(CallbackQueryHandler(telegram_button))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text_router)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    logger.info("Telegram bot polling started")

    yield

    logger.info("Stopping Telegram bot")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


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
    subtitles: Literal["none", "english"] = Field(
        "none", description="Optionally burn English subtitles into the clip"
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


class ViralClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to get every viral-worthy clip Gemini finds (typically 6-10)."
    )
    vertical_crop: bool = Field(True, description="Crop each clip to 9:16 vertical using face detection")
    subtitles: Literal["none", "english"] = Field(
        "english", description="Optionally burn English subtitles into each clip"
    )


def _prepare(url: str):
    try:
        video_id = get_video_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    video_path = download_video(video_id)
    return video_id, video_path


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/subtitles/english")
def subtitles_english(req: SubtitleRequest):
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
def clip(req: ClipRequest):
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

    words = transcribe_words_english(video_id, video_path) if req.subtitles == "english" else None

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


def _analyze_viral_clips(video_id: str, video_path, max_clips: Optional[int]) -> list:
    """Shared by /clips/analyze and /clips/viral: builds a native-language
    transcript and asks Gemini to find every viral-worthy moment, with the
    AI itself deciding how many clips that is (unless max_clips caps it)."""
    native = transcribe_words_native(video_id, video_path)
    transcript_text = words_to_transcript_text(native["words"])
    try:
        candidates = find_viral_clips(transcript_text, max_clips=max_clips)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not candidates:
        raise HTTPException(422, "Gemini did not find any viral-worthy clips in this video")
    return candidates


@app.post("/clips/analyze", response_model=List[ViralClipInfo])
def clips_analyze(req: AnalyzeClipsRequest):
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
):
    if not telegram_bot:
        logger.warning("Telegram bot is not configured")
        return

    caption = (
        f"🎬 New Short Generated\n\n"
        f"🆔 Clip: {clip_id}\n"
        f"🔥 Viral Score: {score}\n\n"
        f"📝 Title:\n{title or 'No title'}\n\n"
        f"💡 Reason:\n{reason or 'N/A'}"
    )

    try:
        with open(video_path, "rb") as video:
            await telegram_bot.send_video(
                chat_id=TELEGRAM_CHAT_ID,
                video=video,
                caption=caption,
            )

        logger.info("Sent clip %s to Telegram", clip_id)

    except TelegramError:
        logger.exception("Failed to send clip %s to Telegram", clip_id)


async def generate_viral_clips(req: ViralClipsRequest):
    """Generate viral clips and send each generated MP4 to Telegram.

    Returns (video_id, entries). All blocking work (download, transcription,
    Gemini analysis, rendering) is pushed to worker threads via
    asyncio.to_thread so this coroutine never blocks the event loop — this
    matters because the same loop also runs the Telegram bot's polling.
    """

    logger.info(
        "viral clips requested url=%s max_clips=%s vertical_crop=%s subtitles=%s",
        req.url,
        req.max_clips,
        req.vertical_crop,
        req.subtitles,
    )

    video_id, video_path = await asyncio.to_thread(_prepare, req.url)

    candidates = await asyncio.to_thread(
        _analyze_viral_clips,
        video_id,
        video_path,
        req.max_clips,
    )

    english_words = (
        await asyncio.to_thread(transcribe_words_english, video_id, video_path)
        if req.subtitles == "english"
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
            words=english_words,
            vertical_crop=req.vertical_crop,
        )

        entries.append({
            "path": output_path,
            "title": candidate.get("title"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "score": candidate.get("score"),
            "reason": candidate.get("reason"),
        })

    if not entries:
        raise HTTPException(
            422,
            "All candidate clips were malformed - nothing to render",
        )

    # Send each generated clip individually to Telegram.
    for i, entry in enumerate(entries, start=1):
        await send_clip_to_telegram(
            video_path=entry["path"],
            clip_id=f"{video_id}-{i}",
            title=entry["title"],
            score=entry["score"],
            reason=entry["reason"],
        )

    logger.info(
        "Generated and sent %d viral clip(s) to Telegram for video_id=%s",
        len(entries),
        video_id,
    )

    return video_id, entries


@app.post("/clips/viral")
async def clips_viral(req: ViralClipsRequest):
    """Auto-find viral clips, render them, send them to Telegram, and return metadata."""

    video_id, entries = await generate_viral_clips(req)

    return {
        "success": True,
        "clips_sent": len(entries),
        "clips": [
            {
                "clip_id": f"{video_id}-{i}",
                "file": entry["path"].name,
                "title": entry["title"],
                "score": entry["score"],
                "reason": entry["reason"],
            }
            for i, entry in enumerate(entries, start=1)
        ],
    }import logging
import time
import uuid
import asyncio
import shutil
import os
from pathlib import Path
from typing import List, Literal, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
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

telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

from logging_config import request_id_var, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from video_utils import (
    CLIPS_DIR,
    TMP_DIR,
    download_video,
    find_viral_clips,
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

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()


async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            InlineKeyboardButton("📝 YES", callback_data="subs:english"),
            InlineKeyboardButton("🚫 NO", callback_data="subs:none"),
        ],
        [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
    ]
    await query.edit_message_text(
        "Do you want subtitles?",
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
    subtitles = context.user_data.get("subtitles", "english")

    if not url:
        await query.edit_message_text(
            "❌ YouTube URL not found. Please start again with /start."
        )
        return

    await query.edit_message_text(
        "⏳ Generating viral Shorts...\n\n"
        f"🎬 Clips: {'AI Decide' if max_clips is None else max_clips}\n"
        f"📝 Subtitles: {'YES' if subtitles == 'english' else 'NO'}\n\n"
        "This may take some time."
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
        video_id, entries = await generate_viral_clips(req)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Done!\n\n🎬 Generated {len(entries)} Shorts.",
        )
    except Exception as e:
        logger.exception("Telegram AI generation failed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Generation failed.\n\nError: {str(e)}",
        )


async def run_timestamp_generation(query, context):
    url = context.user_data.get("youtube_url")
    timestamp = context.user_data.get("timestamp")
    subtitles = context.user_data.get("subtitles", "english")

    if not url or not timestamp:
        await query.edit_message_text(
            "❌ Missing URL or timestamp. Please start again with /start."
        )
        return

    start, end = timestamp

    await query.edit_message_text(
        "⏳ Creating timestamp clip...\n\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {'YES' if subtitles == 'english' else 'NO'}"
    )

    try:
        # All blocking (CPU/network/disk) calls are pushed to a worker thread
        # so the bot's event loop stays free to handle other updates while
        # this clip is being downloaded/transcribed/rendered.
        video_id, video_path = await asyncio.to_thread(_prepare, url)

        words = (
            await asyncio.to_thread(transcribe_words_english, video_id, video_path)
            if subtitles == "english"
            else None
        )

        output_path = (
            CLIPS_DIR / f"{video_id}-timestamp-{uuid.uuid4().hex[:8]}.mp4"
        )

        await asyncio.to_thread(
            render_video,
            video_path,
            output_path,
            time_to_seconds(start),
            time_to_seconds(end),
            words=words,
            vertical_crop=True,
        )

        await send_clip_to_telegram(
            video_path=output_path,
            clip_id=f"{video_id}-timestamp",
            title="Timestamp Clip",
            score=None,
            reason=f"{start} → {end}",
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✅ Timestamp clip generated and sent.",
        )
    except Exception as e:
        logger.exception("Telegram timestamp generation failed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Timestamp generation failed.\n\nError: {str(e)}",
        )


async def telegram_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_timestamp"):
        try:
            start, end = parse_timestamp_range(update.message.text)
            context.user_data["timestamp"] = (start, end)
            context.user_data["waiting_for_timestamp"] = False

            await update.message.reply_text(
                f"⏱️ Timestamp received: {start} → {end}\n\n"
                "Do you want subtitles?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📝 YES", callback_data="subs:english"),
                        InlineKeyboardButton("🚫 NO", callback_data="subs:none"),
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


def _delete_all_files_sync():
    """Blocking filesystem cleanup — run via asyncio.to_thread."""
    deleted_count = 0
    freed_bytes = 0

    folders = [CLIPS_DIR, TMP_DIR]

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
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Cancelled.\n\nSend /start to open the menu."
        )
        return

    if data == "create_shorts":
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("🤖 AI Viral Clips", callback_data="mode_ai")],
            [InlineKeyboardButton("⏱️ Timestamp Clip", callback_data="mode_timestamp")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
        ]
        await query.edit_message_text(
            "🎬 Create Shorts\n\nChoose the clip type:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "delete_menu":
        keyboard = [[
            InlineKeyboardButton("🗑️ DELETE ALL", callback_data="delete_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="cancel"),
        ]]
        await query.edit_message_text(
            "⚠️ Delete all saved files?\n\n"
            "This removes downloaded videos, generated Shorts, "
            "and temporary files. The folders remain.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "delete_confirm":
        await query.edit_message_text("🗑️ Deleting files...")
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
        await query.edit_message_text(
            "🤖 AI Viral Clips\n\nNow send the YouTube video URL."
        )
        return

    if data == "mode_timestamp":
        context.user_data["mode"] = "timestamp"
        await query.edit_message_text(
            "⏱️ Timestamp Clip\n\nNow send the YouTube video URL."
        )
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app.add_handler(CommandHandler("start", telegram_start))
    telegram_app.add_handler(CallbackQueryHandler(telegram_button))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text_router)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    logger.info("Telegram bot polling started")

    yield

    logger.info("Stopping Telegram bot")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


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
    subtitles: Literal["none", "english"] = Field(
        "none", description="Optionally burn English subtitles into the clip"
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


class ViralClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to get every viral-worthy clip Gemini finds (typically 6-10)."
    )
    vertical_crop: bool = Field(True, description="Crop each clip to 9:16 vertical using face detection")
    subtitles: Literal["none", "english"] = Field(
        "english", description="Optionally burn English subtitles into each clip"
    )


def _prepare(url: str):
    try:
        video_id = get_video_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    video_path = download_video(video_id)
    return video_id, video_path


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/subtitles/english")
def subtitles_english(req: SubtitleRequest):
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
def clip(req: ClipRequest):
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

    words = transcribe_words_english(video_id, video_path) if req.subtitles == "english" else None

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


def _analyze_viral_clips(video_id: str, video_path, max_clips: Optional[int]) -> list:
    """Shared by /clips/analyze and /clips/viral: builds a native-language
    transcript and asks Gemini to find every viral-worthy moment, with the
    AI itself deciding how many clips that is (unless max_clips caps it)."""
    native = transcribe_words_native(video_id, video_path)
    transcript_text = words_to_transcript_text(native["words"])
    try:
        candidates = find_viral_clips(transcript_text, max_clips=max_clips)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not candidates:
        raise HTTPException(422, "Gemini did not find any viral-worthy clips in this video")
    return candidates


@app.post("/clips/analyze", response_model=List[ViralClipInfo])
def clips_analyze(req: AnalyzeClipsRequest):
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
):
    if not telegram_bot:
        logger.warning("Telegram bot is not configured")
        return

    caption = (
        f"🎬 New Short Generated\n\n"
        f"🆔 Clip: {clip_id}\n"
        f"🔥 Viral Score: {score}\n\n"
        f"📝 Title:\n{title or 'No title'}\n\n"
        f"💡 Reason:\n{reason or 'N/A'}"
    )

    try:
        with open(video_path, "rb") as video:
            await telegram_bot.send_video(
                chat_id=TELEGRAM_CHAT_ID,
                video=video,
                caption=caption,
            )

        logger.info("Sent clip %s to Telegram", clip_id)

    except TelegramError:
        logger.exception("Failed to send clip %s to Telegram", clip_id)


async def generate_viral_clips(req: ViralClipsRequest):
    """Generate viral clips and send each generated MP4 to Telegram.

    Returns (video_id, entries). All blocking work (download, transcription,
    Gemini analysis, rendering) is pushed to worker threads via
    asyncio.to_thread so this coroutine never blocks the event loop — this
    matters because the same loop also runs the Telegram bot's polling.
    """

    logger.info(
        "viral clips requested url=%s max_clips=%s vertical_crop=%s subtitles=%s",
        req.url,
        req.max_clips,
        req.vertical_crop,
        req.subtitles,
    )

    video_id, video_path = await asyncio.to_thread(_prepare, req.url)

    candidates = await asyncio.to_thread(
        _analyze_viral_clips,
        video_id,
        video_path,
        req.max_clips,
    )

    english_words = (
        await asyncio.to_thread(transcribe_words_english, video_id, video_path)
        if req.subtitles == "english"
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
            words=english_words,
            vertical_crop=req.vertical_crop,
        )

        entries.append({
            "path": output_path,
            "title": candidate.get("title"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "score": candidate.get("score"),
            "reason": candidate.get("reason"),
        })

    if not entries:
        raise HTTPException(
            422,
            "All candidate clips were malformed - nothing to render",
        )

    # Send each generated clip individually to Telegram.
    for i, entry in enumerate(entries, start=1):
        await send_clip_to_telegram(
            video_path=entry["path"],
            clip_id=f"{video_id}-{i}",
            title=entry["title"],
            score=entry["score"],
            reason=entry["reason"],
        )

    logger.info(
        "Generated and sent %d viral clip(s) to Telegram for video_id=%s",
        len(entries),
        video_id,
    )

    return video_id, entries


@app.post("/clips/viral")
async def clips_viral(req: ViralClipsRequest):
    """Auto-find viral clips, render them, send them to Telegram, and return metadata."""

    video_id, entries = await generate_viral_clips(req)

    return {
        "success": True,
        "clips_sent": len(entries),
        "clips": [
            {
                "clip_id": f"{video_id}-{i}",
                "file": entry["path"].name,
                "title": entry["title"],
                "score": entry["score"],
                "reason": entry["reason"],
            }
            for i, entry in enumerate(entries, start=1)
        ],
    }