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
    crop and/or burned-in English subtitles."""
    logger.info(
        "clip requested url=%s start=%s end=%s vertical_crop=%s subtitles=%s",
        req.url, req.start, req.end, req.vertical_crop, req.subtitles,
    )
    video_id, video_path = prepare(req.url)
    start_sec = time_to_seconds(req.start)
    end_sec = time_to_seconds(req.end)
    if end_sec <= start_sec:
        raise HTTPException(400, "end must be after start")

    words = resolve_subtitle_words(video_id, video_path, req.subtitles)

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")
