import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.deps import limiter
from app.api.schemas import ClipRequest
from app.config import CLIPS_DIR
from app.services.video.render import render_video
from app.services.video.timeutils import time_to_seconds
from app.services.viral import prepare, resolve_subtitle_words

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/clip")
@limiter.limit("20/minute")
def clip(request: Request, req: ClipRequest):
    """Cuts [start, end] out of the video, optionally with a 9:16 face-aware
    crop and/or burned-in subtitles (hinglish default)."""
    logger.info(
        "clip requested url=%s start=%s end=%s vertical_crop=%s subtitles=%s",
        req.url, req.start, req.end, req.vertical_crop, req.subtitles,
    )
    try:
        start_sec = time_to_seconds(req.start)
        end_sec = time_to_seconds(req.end)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"invalid start/end: {e}")
    if end_sec <= start_sec:
        raise HTTPException(400, "end must be after start")
    max_dur = float(getattr(__import__("app.config", fromlist=["settings"]).settings, "MAX_CLIP_DURATION_SEC", 60.0) or 60.0)
    # Manual clips: allow up to 3x max (180s) for long-form cuts, but reject
    # absurd 10-min ranges that OOM/timeout and can't send via Telegram.
    if end_sec - start_sec > max_dur * 3:
        raise HTTPException(400, f"clip too long ({end_sec - start_sec:.0f}s > {max_dur * 3:.0f}s max for /clip)")
    video_id, video_path = prepare(req.url)
    words = resolve_subtitle_words(video_id, video_path, req.subtitles)
    if req.subtitles != "none" and words is not None:
        try:
            from app.config import settings as _settings
            from app.services.video.timeutils import snap_to_silence
            ns, ne = snap_to_silence(
                start_sec, end_sec, words=words,
                window_sec=min(1.5, _settings.SNAP_WINDOW_SEC + 0.5),
            )
            if ne > ns:
                start_sec, end_sec = ns, ne
        except Exception:
            pass
        has_overlap = any(
            (lambda ws, we: we > start_sec and ws < end_sec)(
                float(w.get("start", 0)), float(w.get("end", 0))
            ) for w in words
            if isinstance(w, dict)
        )
        if not has_overlap:
            logger.warning("No subtitle words overlap [%.1f, %.1f] for video_id=%s", start_sec, end_sec, video_id)

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")
