import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.deps import limiter
from app.api.schemas import ClipRequest
from app.config import CLIPS_DIR
from app.services.video.render import render_video
from app.services.video.timeutils import time_to_seconds
from app.services.viral import analyze_viral_clips, pick_title_for_range, prepare, resolve_subtitle_words

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/clip")
@limiter.limit("20/minute")
def clip(request: Request, req: ClipRequest):
    """Cuts [start, end] out of the video, optionally with a 9:16 face-aware
    crop and/or burned-in English subtitles."""
    logger.info(
        "clip requested url=%s start=%s end=%s vertical_crop=%s processing_mode=%s subtitles=%s",
        req.url, req.start, req.end, req.vertical_crop, req.processing_mode, req.subtitles,
    )
    video_id, video_path = prepare(req.url)
    start_sec = time_to_seconds(req.start)
    end_sec = time_to_seconds(req.end)
    if end_sec <= start_sec:
        raise HTTPException(400, "end must be after start")

    words = resolve_subtitle_words(video_id, video_path, req.subtitles)

    # Upper AI title: only in 9:16 no-face mode. Explicit `title` wins;
    # otherwise reuse the AI viral analysis (nearest overlapping clip).
    # Failures fall back to no-title render — never fail the clip.
    clip_title: str | None = None
    if req.vertical_crop and req.processing_mode == "no_face_detection":
        explicit = (req.title or "").strip()
        if explicit:
            clip_title = explicit
        else:
            try:
                candidates = analyze_viral_clips(video_id, video_path, None)
                clip_title = pick_title_for_range(candidates, start_sec, end_sec)
            except Exception:
                logger.exception(
                    "Auto-title lookup failed for video_id=%s, rendering without title",
                    video_id,
                )
                clip_title = None

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_kwargs: dict = {
        "words": words,
        "vertical_crop": req.vertical_crop,
        "processing_mode": req.processing_mode,
    }
    if clip_title:
        render_kwargs["title"] = clip_title
    render_video(video_path, output_path, start_sec, end_sec, **render_kwargs)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")
