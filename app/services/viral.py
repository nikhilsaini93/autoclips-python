import asyncio
import logging
import uuid

from fastapi import HTTPException

from app.config import CLIPS_DIR, settings
from app.services.jobs import update_job
from app.services.video.download import download_video, get_video_credit, get_video_duration
from app.services.video.gemini import find_viral_clips
from app.services.video.ids import get_video_id
from app.services.video.render import render_video
from app.services.video.romanize import to_hinglish_words
from app.services.video.timeutils import seconds_to_time, snap_to_silence, time_to_seconds
from app.services.video.transcribe import (
    peek_cached_transcript,
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
    return {"english": "English", "native": "Native", "hinglish": "Hinglish", "none": "NO"}.get(value, value)


def resolve_subtitle_words(video_id: str, video_path, subtitles: str):
    """Returns word list for burning, or None. 'native' reuses the
    as-spoken transcript (timestamps align exactly with clip boundaries);
    'english' uses Whisper's translate task; 'hinglish' is the native words
    romanized to Latin script (same timings — Hindi audio reads as Hinglish,
    English audio passes through untouched)."""
    if subtitles == "english":
        # No translate pass when the audio is already English: the native
        # transcript (always built + cached by analyze/viral flows, carrying
        # the detected language) is reused with exact native timings.
        # Peek only — never transcribes here, so first-time /clip calls keep
        # today's single-pass behavior.
        cached_native = peek_cached_transcript(video_id, task="transcribe")
        if cached_native and (cached_native.get("language") or "").lower().startswith("en"):
            if cached_native.get("words"):
                logger.info("Audio already English — reusing native transcript, "
                            "skipping Whisper translate pass for video_id=%s", video_id)
                return cached_native["words"]
        return transcribe_words_english(video_id, video_path)
    if subtitles == "hinglish":
        return to_hinglish_words(transcribe_words_native(video_id, video_path)["words"])
    if subtitles == "native":
        return transcribe_words_native(video_id, video_path)["words"]
    return None


def analyze_viral_clips(video_id: str, video_path, max_clips) -> list:
    """Shared by analyze + viral flows: builds a native-language transcript
    and asks Gemini for Hinglish titles/descriptions, then wraps each
    description with Credit + Original link + fair-use disclaimer."""
    native = transcribe_words_native(video_id, video_path)
    detected_language = native.get("language")
    words = native.get("words") or []
    segments = native.get("segments") or []
    silences = native.get("silences") or []
    transcript_text = words_to_transcript_text(words, segments=segments, silences=silences)
    logger.info("clip analysis language=%s video_id=%s (%d words, %d segments, %d silences)",
                detected_language, video_id, len(words), len(segments), len(silences))
    try:
        candidates = find_viral_clips(transcript_text, max_clips=max_clips, language=detected_language)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not candidates:
        raise HTTPException(422, "Gemini did not find any viral-worthy clips in this video")
    try:
        duration_sec = get_video_duration(video_path)
    except Exception:
        duration_sec = None
    max_dur = float(getattr(settings, "MAX_CLIP_DURATION_SEC", 60.0) or 60.0)
    min_dur = float(getattr(settings, "MIN_CLIP_DURATION_SEC", 15.0) or 15.0)
    if settings.SNAP_TO_SILENCE and (words or segments or silences):
        for c in candidates:
            try:
                raw_start = time_to_seconds(c["start"])
                raw_end = time_to_seconds(c["end"])
            except (KeyError, ValueError, TypeError):
                continue
            ns, ne = snap_to_silence(
                raw_start, raw_end, words=words, silences=silences, segments=segments,
                window_sec=settings.SNAP_WINDOW_SEC, duration_sec=duration_sec,
            )
            if (ns, ne) != (raw_start, raw_end):
                logger.info("Snapped clip '%s': [%.1f -> %.1f] to [%.1f -> %.1f]",
                            c.get("title"), raw_start, raw_end, ns, ne)
                c["start"] = seconds_to_time(ns)
                c["end"] = seconds_to_time(ne)
    # Enforce duration guards + clamp overlong (Gemini sometimes returns
    # 120s+ despite its 20-60s rule — those fail Telegram 50MB + FB 90s).
    # Also clamp end to source duration so ffmpeg never gets a short file.
    filtered: list = []
    for c in candidates:
        try:
            ss, se = time_to_seconds(c["start"]), time_to_seconds(c["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if duration_sec is not None:
            try:
                se = min(se, float(duration_sec))
            except (TypeError, ValueError):
                pass
        if se <= ss:
            continue
        if se - ss > max_dur:
            logger.warning("Clamping overlong clip '%s' %.1fs -> %.1fs",
                           c.get("title"), se - ss, max_dur)
            se = ss + max_dur
            c["end"] = seconds_to_time(se)
        if se - ss < min(8.0, min_dur):
            logger.warning("Dropping too-short clip '%s' %.1fs", c.get("title"), se - ss)
            continue
        filtered.append(c)
    candidates = filtered
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
    max_dur = float(getattr(settings, "MAX_CLIP_DURATION_SEC", 60.0) or 60.0)

    # Pre-compute clamped ranges + re-snap to the *burn* words when english
    # subs are used (native snap + english burn drifted 0.3-1s before).
    jobs_list: list[tuple[int, dict, float, float, object]] = []
    for i, candidate in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(candidate["start"])
            end_sec = time_to_seconds(candidate["end"])
        except (KeyError, ValueError, TypeError) as e:
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
        if end_sec - start_sec > max_dur:
            logger.warning("Clamping viral clip #%d %.1fs -> %.1fs", i, end_sec - start_sec, max_dur)
            end_sec = start_sec + max_dur
            candidate["end"] = seconds_to_time(end_sec)
        # Re-snap to burn words so cuts line up with what's actually burned.
        if settings.SNAP_TO_SILENCE and subtitle_words and req.subtitles == "english":
            try:
                ns, ne = snap_to_silence(
                    start_sec, end_sec, words=subtitle_words,
                    silences=[], segments=[],
                    window_sec=min(1.5, settings.SNAP_WINDOW_SEC + 0.5),
                )
                if ne > ns and abs(ns - start_sec) + abs(ne - end_sec) < 3.0:
                    start_sec, end_sec = ns, ne
            except Exception:
                pass
        output_path = (
            CLIPS_DIR
            / f"{video_id}-viral-{i}-{uuid.uuid4().hex[:8]}.mp4"
        )
        jobs_list.append((i, candidate, start_sec, end_sec, output_path))

    # Render with bounded parallelism (2 at a time): 10-20min serial batch
    # becomes ~2x faster without starving Whisper/CPU. Order preserved.
    import asyncio as _asyncio

    _sem = _asyncio.Semaphore(2)

    async def _render_one(item):
        i, candidate, start_sec, end_sec, output_path = item
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
        try:
            await _asyncio.to_thread(
                render_video,
                video_path,
                output_path,
                start_sec,
                end_sec,
                words=subtitle_words,
                vertical_crop=req.vertical_crop,
            )
        except Exception:
            logger.exception("Render failed for clip %s-%d, continuing batch", video_id, i)
            return None
        return (i, candidate, start_sec, end_sec, output_path)

    async def _guarded(item):
        async with _sem:
            return await _render_one(item)

    rendered = await _asyncio.gather(*[_guarded(j) for j in jobs_list])
    for item in sorted([r for r in rendered if r is not None], key=lambda x: x[0]):
        i, candidate, start_sec, end_sec, output_path = item
        entries.append({
            "path": output_path,
            "title": candidate.get("title"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "duration_seconds": round(end_sec - start_sec, 1),
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
