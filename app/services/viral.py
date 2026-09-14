import asyncio
import logging
import uuid

from fastapi import HTTPException

from app.config import CLIPS_DIR
from app.services.jobs import update_job
from app.services.video.download import download_video, get_video_credit
from app.services.video.gemini import find_viral_clips
from app.services.video.ids import get_video_id
from app.services.video.render import render_video
from app.services.video.timeutils import time_to_seconds
from app.services.video.transcribe import (
    transcribe_words_english,
    transcribe_words_native,
    words_to_transcript_text,
)
from app.services.youtube.descriptions import build_yt_description

logger = logging.getLogger(__name__)


def prepare(url: str):
    try:
        video_id = get_video_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    video_path = download_video(video_id)
    return video_id, video_path


def subtitle_label(value: str) -> str:
    return {"english": "English", "native": "Native", "none": "NO"}.get(value, value)


def resolve_subtitle_words(video_id: str, video_path, subtitles: str):
    """Returns word list for burning, or None. 'native' reuses the
    as-spoken transcript (timestamps align exactly with clip boundaries);
    'english' uses Whisper's translate task."""
    if subtitles == "english":
        return transcribe_words_english(video_id, video_path)
    if subtitles == "native":
        return transcribe_words_native(video_id, video_path)["words"]
    return None


def analyze_viral_clips(video_id: str, video_path, max_clips) -> list:
    """Shared by analyze + viral flows: builds a native-language transcript
    and asks Gemini for Hinglish titles/descriptions, then wraps each
    description with Credit + Original link + fair-use disclaimer."""
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


async def generate_viral_clips(req, job_id: str | None = None):
    """Generate viral clips and send each generated MP4 to Telegram.

    Returns (video_id, entries). All blocking work (download, transcription,
    Gemini analysis, rendering) is pushed to worker threads via
    asyncio.to_thread so this coroutine never blocks the event loop — this
    matters because the same loop also runs the Telegram bot's polling.
    """
    # Local import to avoid api <-> telegram cycle at module load.
    from app.telegram.sending import send_clip_to_telegram

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
    video_id, video_path = await asyncio.to_thread(prepare, req.url)

    _progress("transcribing", percent=15, stage="transcribing",
              detail="native transcript for AI analysis (slowest step on CPU)")
    _progress("analyzing", percent=20, stage="analyzing")
    candidates = await asyncio.to_thread(
        analyze_viral_clips,
        video_id,
        video_path,
        req.max_clips,
    )

    # Gemini count is now known — clip-phase % can be computed honestly.
    total = len(candidates)
    if job_id:
        from app.services.jobs import jobs
        jobs[job_id].update({"total_clips": total, "done_clips": 0})

    _progress("transcribing" if req.subtitles != "none" else "rendering",
              percent=68 if req.subtitles != "none" else 72,
              stage="transcribing" if req.subtitles != "none" else "rendering")
    subtitle_words = (
        await asyncio.to_thread(resolve_subtitle_words, video_id, video_path, req.subtitles)
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
            from app.services.jobs import jobs
            jobs[job_id].update({
                "done_clips": len(entries),
                "percent": 75 + int(25 * len(entries) / max(1, len(candidates))),
            })

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
