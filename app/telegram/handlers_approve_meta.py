"""Telegram Approve handlers for Instagram Reels + Facebook Reels.

Mirrors handlers_approve.handle_approve (YouTube): single-use token, job
tracking, keep-file-on-failure with a Retry button. Success is reported via
a NEW message so the original clip message keeps its other Approve buttons
(multi-platform uploads from one clip).
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from app.services.approvals import pop_pending_upload, register_pending_upload
from app.services.jobs import create_job, update_job

logger = logging.getLogger(__name__)


def _retry_keyboard(platform: str, retry_token: str) -> InlineKeyboardMarkup:
    if platform == "instagram":
        label, deny = "📸 Retry IG upload", f"deny:{retry_token}"
        approve = f"approve_ig:{retry_token}"
    else:
        label, deny = "📘 Retry FB upload", f"deny:{retry_token}"
        approve = f"approve_fb:{retry_token}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=approve),
        InlineKeyboardButton("❌ Deny (delete)", callback_data=deny),
    ]])


async def handle_approve_ig(update, context, token: str):
    """Uploads the approved clip as an Instagram Reel (single-use token)."""
    query = update.callback_query
    chat_id = query.message.chat_id if query.message else None
    entry = pop_pending_upload(token)
    if entry is None:
        await query.answer("⏳ Approval expired (bot restarted?). Clip file is untouched.", show_alert=True)
        return
    await query.answer("Uploading to Instagram as Reel…")
    job_id = create_job("ig_upload")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Uploading {entry['clip_id']} to Instagram as Reel…\n🆔 Job: {job_id}",
        )
    except TelegramError:
        pass
    try:
        from app.services.meta.instagram import upload_instagram_reel

        result = await asyncio.to_thread(
            upload_instagram_reel,
            entry["path"],
            entry.get("title"),
            entry.get("description"),
            entry.get("hashtags"),
            entry.get("source_video_id") or None,
            None,
            entry.get("credit") or None,
        )
        update_job(job_id, status="done", result=result)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Instagram Reel published\n\n🎯 {entry['title']}\n"
                    f"🔗 {result.get('url') or result.get('media_id', '')}\n🆔 {entry['clip_id']}"
                ),
            )
        except TelegramError:
            logger.debug("Could not send IG success message for %s", entry["clip_id"])
        logger.info("IG approve done clip=%s media=%s", entry["clip_id"], result.get("media_id"))
    except Exception as e:
        logger.exception("Instagram upload failed for %s", entry["clip_id"])
        update_job(job_id, status="failed", error=str(e))
        retry_token = register_pending_upload(
            entry["path"], entry["clip_id"], entry["title"],
            entry.get("description"), entry.get("hashtags"), chat_id,
            source_video_id=entry.get("source_video_id"), credit=entry.get("credit"),
            platform="instagram",
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ IG upload failed for {entry['clip_id']}.\n\nError: {str(e)}\n"
                    "The clip file was kept — tap Retry to try again."
                ),
                reply_markup=_retry_keyboard("instagram", retry_token),
            )
        except TelegramError:
            pass


async def handle_approve_fb(update, context, token: str):
    """Uploads the approved clip as a Facebook Page Reel (single-use token)."""
    query = update.callback_query
    chat_id = query.message.chat_id if query.message else None
    entry = pop_pending_upload(token)
    if entry is None:
        await query.answer("⏳ Approval expired (bot restarted?). Clip file is untouched.", show_alert=True)
        return
    await query.answer("Uploading to Facebook as Reel…")
    job_id = create_job("fb_upload")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Uploading {entry['clip_id']} to Facebook as Reel…\n🆔 Job: {job_id}",
        )
    except TelegramError:
        pass
    try:
        from app.services.meta.facebook import upload_facebook_reel

        result = await asyncio.to_thread(
            upload_facebook_reel,
            entry["path"],
            entry.get("title"),
            entry.get("description"),
            entry.get("hashtags"),
            entry.get("source_video_id") or None,
            None,
            entry.get("credit") or None,
        )
        update_job(job_id, status="done", result=result)
        kind = result.get("kind") or "reel"
        extra = " (posted as regular video — clip >90s)" if result.get("fallback") else ""
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Facebook {kind} published{extra}\n\n🎯 {entry['title']}\n"
                    f"🔗 {result.get('url') or result.get('post_id', '')}\n🆔 {entry['clip_id']}"
                ),
            )
        except TelegramError:
            logger.debug("Could not send FB success message for %s", entry["clip_id"])
        logger.info("FB approve done clip=%s post=%s", entry["clip_id"], result.get("post_id"))
    except Exception as e:
        logger.exception("Facebook upload failed for %s", entry["clip_id"])
        update_job(job_id, status="failed", error=str(e))
        retry_token = register_pending_upload(
            entry["path"], entry["clip_id"], entry["title"],
            entry.get("description"), entry.get("hashtags"), chat_id,
            source_video_id=entry.get("source_video_id"), credit=entry.get("credit"),
            platform="facebook",
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ FB upload failed for {entry['clip_id']}.\n\nError: {str(e)}\n"
                    "The clip file was kept — tap Retry to try again."
                ),
                reply_markup=_retry_keyboard("facebook", retry_token),
            )
        except TelegramError:
            pass
