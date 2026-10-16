"""Telegram flow for the YouTube splitter: URL -> part length -> 9:16/16:9 ->
summary + confirm -> download/split/convert with live progress messages.

No face detection, no subtitles; generated files stay under
storage/splitter/{job_id}/ and are NOT sent to Telegram — the bot only reports
progress and the final part list with durations/sizes/paths.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from app.services.jobs import create_job
from app.services.splitter import (
    output_size,
    parse_part_minutes,
    plan_parts,
    run_splitter_job,
)
from app.services.video.download import get_video_metadata
from app.services.video.ids import get_video_id
from app.telegram.progress import format_split_progress, progress_bar, run_progress_editor

logger = logging.getLogger(__name__)

RATIO_LABELS = {
    "9:16": "9:16 vertical (1080x1920)",
    "16:9": "16:9 landscape (1920x1080)",
}
MAX_LISTED_PARTS = 30


def _fmt_ts(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds))))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _ratio_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 9:16 — Vertical (1080x1920)", callback_data="split_ratio:9:16")],
        [InlineKeyboardButton("🖥️ 16:9 — Landscape (1920x1080)", callback_data="split_ratio:16:9")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
    ])


async def start_split_flow(update, context):
    """Validates the URL and fetches duration/metadata, then asks for part length."""
    url = context.user_data["youtube_url"]
    try:
        video_id = get_video_id(url)
    except ValueError:
        await update.message.reply_text("❌ Please send a valid YouTube URL.")
        return

    await update.message.reply_text("🔎 Checking video...")
    try:
        meta = await asyncio.to_thread(get_video_metadata, video_id)
    except RuntimeError as e:
        await update.message.reply_text(
            f"❌ Could not access this video.\n\n{e}\n\n"
            "Private, age-restricted or region-locked videos need a fresh "
            "cookies.txt (YOUTUBE_COOKIES_FILE)."
        )
        return

    context.user_data.update({
        "split_video_id": video_id,
        "split_duration": float(meta["duration"]),
        "split_title": meta.get("title") or "",
        "waiting_for_split_minutes": True,
    })
    await update.message.reply_text(
        f"🎥 {meta.get('title') or video_id}\n"
        f"⏱️ Duration: {_fmt_ts(meta['duration'])}\n\n"
        "✂️ Send the length of each part in minutes (e.g. 2 or 1.5)."
    )


async def handle_split_minutes_input(update, context):
    duration = context.user_data.get("split_duration")
    if not duration:
        await update.message.reply_text("❌ Session expired. Send /start to begin again.")
        return
    try:
        part_seconds = parse_part_minutes(update.message.text)
        parts = plan_parts(duration, part_seconds)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    context.user_data["split_part_seconds"] = part_seconds
    context.user_data["waiting_for_split_minutes"] = False
    last = parts[-1][1] - parts[-1][0]
    last_note = f" (last part {last:.0f}s)" if abs(last - part_seconds) > 0.5 else ""
    await update.message.reply_text(
        f"✂️ {len(parts)} part(s) of {part_seconds / 60:g} min{last_note}.\n\n"
        "Choose the output format:",
        reply_markup=_ratio_keyboard(),
    )


async def ask_split_confirm(query, context, ratio: str):
    """Shows the options summary + Confirm/Cancel for the chosen aspect ratio."""
    duration = context.user_data.get("split_duration")
    part_seconds = context.user_data.get("split_part_seconds")
    if ratio not in RATIO_LABELS or not duration or not part_seconds:
        await query.edit_message_text("❌ Session expired. Send /start to begin again.")
        return
    try:
        parts = plan_parts(duration, part_seconds)
    except ValueError as e:
        await query.edit_message_text(f"❌ {e}\n\nSend /start to begin again.")
        return

    context.user_data["split_aspect_ratio"] = ratio
    width, height = output_size(ratio)
    last = parts[-1][1] - parts[-1][0]
    last_note = f" (last {last:.0f}s)" if abs(last - part_seconds) > 0.5 else ""
    title = context.user_data.get("split_title") or context.user_data.get("split_video_id") or ""
    summary = (
        "✂️ Split summary\n\n"
        f"🎥 {title}\n"
        f"⏱️ Duration: {_fmt_ts(duration)}\n"
        f"🧩 Parts: {len(parts)} x {part_seconds / 60:g} min{last_note}\n"
        f"📐 Output: {ratio} — {width}x{height}, black padding\n\n"
        "Proceed?"
    )
    await query.edit_message_text(
        summary,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="split_confirm")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel")],
        ]),
    )


def _done_text(manifest: dict, job_id: str) -> str:
    parts = manifest.get("parts", [])
    lines = []
    for p in parts[:MAX_LISTED_PARTS]:
        status = "✅" if p.get("status") == "done" else "❌"
        size = f"{p['size_mb']:.1f} MB" if p.get("size_mb") else "—"
        lines.append(
            f"{status} {p.get('filename')} — {_fmt_ts(p.get('start', 0))}–{_fmt_ts(p.get('end', 0))} — {size}"
        )
    remaining = len(parts) - len(lines)
    if remaining > 0:
        lines.append(f"…and {remaining} more (see manifest.json)")
    return (
        f"✅ Done! {len(parts)} part(s) created.\n\n"
        + "\n".join(lines)
        + f"\n\n📁 storage/splitter/{job_id}/ (not sent to Telegram)\n🆔 Job: {job_id}"
    )


async def run_split_generation(query, context):
    url = context.user_data.get("youtube_url")
    video_id = context.user_data.get("split_video_id")
    duration = context.user_data.get("split_duration")
    part_seconds = context.user_data.get("split_part_seconds")
    ratio = context.user_data.get("split_aspect_ratio")
    title = context.user_data.get("split_title") or None

    if not (url and video_id and duration and part_seconds and ratio):
        await query.edit_message_text(
            "❌ Missing split options. Please start again with /start."
        )
        return
    try:
        total = len(plan_parts(duration, part_seconds))
    except ValueError as e:
        await query.edit_message_text(f"❌ {e}\n\nSend /start to begin again.")
        return

    job_id = create_job("splitter")
    await query.edit_message_text(
        "⏳ Splitting video into parts...\n\n"
        f"{progress_bar(0)}\n"
        f"📥 Downloading video...\n"
        f"🧩 Parts: {total} x {part_seconds / 60:g} min\n"
        f"📐 Output: {RATIO_LABELS[ratio]}\n"
        f"🆔 Job: {job_id}\n\n"
        "Parts are saved on disk — they are not sent to Telegram."
    )

    chat_id = query.message.chat_id if query.message else None
    message_id = query.message.message_id if query.message else None
    progress_task = None
    if chat_id is not None and message_id is not None:
        progress_task = asyncio.create_task(
            run_progress_editor(
                context.bot, chat_id, message_id, job_id,
                lambda job: format_split_progress(job, ratio),
            )
        )

    try:
        manifest = await asyncio.to_thread(
            run_splitter_job, job_id, video_id, part_seconds / 60.0, ratio, title,
        )
        if manifest.get("status") != "done":
            raise RuntimeError(manifest.get("error") or "Splitting failed")

        done_text = _done_text(manifest, job_id)
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
        logger.exception("Telegram split generation failed")
        fail_text = f"❌ Splitting failed.\n\nError: {str(e)}\n🆔 Job: {job_id}"
        if progress_task is not None:
            progress_task.cancel()
        if chat_id is not None and message_id is not None:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=fail_text)
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=fail_text)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=fail_text)
