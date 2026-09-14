import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from app.services.approvals import pop_pending_upload, register_pending_upload
from app.services.jobs import create_job, update_job

logger = logging.getLogger(__name__)


async def handle_approve(update, context, token: str):
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
        from app.services.youtube.uploads import upload_unlisted

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


async def handle_deny(update, context, token: str):
    """Deletes the denied clip MP4 to free space (single-use token)."""
    from pathlib import Path

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
