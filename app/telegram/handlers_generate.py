import asyncio
import logging
import uuid

from telegram.error import TelegramError

from app.api.schemas import ViralClipsRequest
from app.config import CLIPS_DIR
from app.services.jobs import create_job, update_job
from app.services.video.render import render_video
from app.services.video.timeutils import time_to_seconds
from app.services.viral import (
    generate_viral_clips,
    prepare,
    resolve_subtitle_words,
    subtitle_label,
)
from app.telegram.progress import (
    format_ai_progress,
    format_timestamp_progress,
    progress_bar,
    run_progress_editor,
)
from app.telegram.sending import send_clip_to_telegram

logger = logging.getLogger(__name__)


def _friendly_error(e: Exception) -> str:
    """Translate yt-dlp/network failures into actionable Telegram text.

    Raw RuntimeErrors leak local paths + 4-attempt command lines; users need
    the fix, not the traceback. Bot-checks (datacenter IP blocks) are by far
    the most common cause on Colab/Kaggle.
    """
    msg = str(e) or ""
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return (
            "❌ YouTube blocked this download (bot-check on the server's IP).\n\n"
            "Fix (once): export cookies.txt from your logged-in desktop browser "
            "(extension 'Get cookies.txt LOCALES' on youtube.com), then:\n"
            "• Colab: upload it to /content/drive/MyDrive/autoclips-config/cookies.txt, "
            "set YOUTUBE_COOKIES_FILE to that path in .env, restart the server cell.\n"
            "• Kaggle: upload to /kaggle/working/cookies.txt and re-run cells 4–8.\n"
            "• Local: set YOUTUBE_COOKIES_FILE=cookies.txt and restart.\n\n"
            "Also re-upload the latest repo zip (old builds passed a dead "
            "--js-runtimes node:/tools/node/bin/node path). "
            "Or send the MP4 directly to skip YouTube."
        )
    if "no working js runtime" in low or "js runtime" in low:
        return (
            "❌ Video download needs a JavaScript runtime (yt-dlp requirement).\n\n"
            "Fix: Colab/Kaggle re-run the system-deps cell "
            "('apt-get install -y nodejs'), Docker already has it, "
            "Windows install Node.js LTS — then restart with the latest code."
        )
    # Fallback: first line only, no local paths / command dumps.
    first = msg.splitlines()[0][:500] if msg else "unknown error"
    return f"❌ Generation failed.\n\nError: {first}"


async def run_ai_generation(query, context):
    url = context.user_data.get("youtube_url")
    max_clips = context.user_data.get("max_clips")
    subtitles = context.user_data.get("subtitles", "hinglish")

    if not url:
        await query.edit_message_text(
            "❌ YouTube URL not found. Please start again with /start."
        )
        return

    job_id = create_job("ai_viral")
    await query.edit_message_text(
        "⏳ Generating viral Shorts...\n\n"
        f"{progress_bar(0)}\n"
        f"📥 Downloading video...\n"
        f"🎬 Clips: {'AI Decide' if max_clips is None else max_clips}\n"
        f"📝 Subtitles: {subtitle_label(subtitles)}\n"
        f"🆔 Job: {job_id}\n\n"
        "This may take a while for long videos."
    )

    chat_id = query.message.chat_id if query.message else None
    message_id = query.message.message_id if query.message else None
    progress_task = None
    if chat_id is not None and message_id is not None:
        progress_task = asyncio.create_task(
            run_progress_editor(
                context.bot,
                chat_id,
                message_id,
                job_id,
                lambda job: format_ai_progress(job, max_clips, subtitles),
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
        fail_text = f"{_friendly_error(e)}\n🆔 Job: {job_id}"
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
    subtitles = context.user_data.get("subtitles", "hinglish")

    if not url or not timestamp:
        await query.edit_message_text(
            "❌ Missing URL or timestamp. Please start again with /start."
        )
        return

    start, end = timestamp

    job_id = create_job("timestamp")
    await query.edit_message_text(
        "⏳ Creating timestamp clip...\n\n"
        f"{progress_bar(0)}\n"
        f"📥 Downloading video...\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {subtitle_label(subtitles)}\n"
        f"🆔 Job: {job_id}"
    )

    chat_id = query.message.chat_id if query.message else None
    message_id = query.message.message_id if query.message else None
    progress_task = None
    if chat_id is not None and message_id is not None:
        update_job(job_id, stage="downloading", percent=2)
        progress_task = asyncio.create_task(
            run_progress_editor(
                context.bot,
                chat_id,
                message_id,
                job_id,
                lambda job: format_timestamp_progress(job, start, end, subtitles),
            )
        )

    try:
        # All blocking (CPU/network/disk) calls are pushed to a worker thread
        # so the bot's event loop stays free to handle other updates while
        # this clip is being downloaded/transcribed/rendered.
        update_job(job_id, progress="downloading", stage="downloading", percent=5)
        video_id, video_path = await asyncio.to_thread(prepare, url)

        update_job(job_id, progress="transcribing" if subtitles != "none" else "rendering",
                   stage="transcribing" if subtitles != "none" else "rendering",
                   percent=40 if subtitles != "none" else 60)
        words = (
            await asyncio.to_thread(resolve_subtitle_words, video_id, video_path, subtitles)
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
        fail_text = f"{_friendly_error(e)}\n🆔 Job: {job_id}"
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=fail_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=fail_text)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=fail_text)
