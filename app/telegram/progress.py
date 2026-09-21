import asyncio
import logging
import time

from telegram.error import RetryAfter, TelegramError, TimedOut

from app.services.jobs import jobs
from app.services.viral import subtitle_label

logger = logging.getLogger(__name__)


def progress_bar(percent: int, width: int = 10) -> str:
    """Renders e.g. '██████░░░░ 62%' for Telegram status messages."""
    try:
        pct = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        pct = 0
    filled = round(pct / 100 * width)
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


def format_elapsed(job: dict) -> str:
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


def format_ai_progress(job: dict, max_clips, subtitles: str) -> str:
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
        f"{progress_bar(percent)}\n"
        f"{label}{counter}\n"
        f"📝 Subtitles: {subtitle_label(subtitles)}\n"
        f"⏱️ Elapsed: {format_elapsed(job)}\n"
        f"🆔 Job: {job.get('job_id', '')}"
    )


def format_timestamp_progress(job: dict, start: str, end: str, subtitles: str) -> str:
    percent = int(job.get("percent", 0) or 0)
    stage = job.get("stage", "") or job.get("progress", "")
    label = _STAGE_LABELS.get(stage, stage or "Working...")
    return (
        "⏳ Creating timestamp clip...\n\n"
        f"{progress_bar(percent)}\n"
        f"{label}\n"
        f"⏱️ {start} → {end}\n"
        f"📝 Subtitles: {subtitle_label(subtitles)}\n"
        f"⏱️ Elapsed: {format_elapsed(job)}\n"
        f"🆔 Job: {job.get('job_id', '')}"
    )


async def run_progress_editor(bot, chat_id, message_id, job_id: str, build_text, interval: float = 15.0):
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
