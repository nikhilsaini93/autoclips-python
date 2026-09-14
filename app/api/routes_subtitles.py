import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.deps import limiter
from app.api.schemas import SubtitleRequest
from app.config import CLIPS_DIR, TMP_DIR
from app.services.video.download import get_video_duration
from app.services.video.render import render_video
from app.services.video.subtitles import write_srt
from app.services.video.transcribe import transcribe_words_english
from app.services.viral import prepare

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/subtitles/english")
@limiter.limit("20/minute")
def subtitles_english(request: Request, req: SubtitleRequest):
    """Transcribes the video and translates it to English (works even if the
    source audio is Hindi/Urdu/etc, via Whisper's translate task)."""
    logger.info("english subtitles requested url=%s burn_in=%s vertical_crop=%s", req.url, req.burn_in, req.vertical_crop)
    video_id, video_path = prepare(req.url)
    words = transcribe_words_english(video_id, video_path)
    duration = get_video_duration(video_path)

    if not req.burn_in:
        srt_path = TMP_DIR / f"{video_id}-english.srt"
        if not write_srt(words, 0.0, duration, srt_path):
            raise HTTPException(422, "No speech detected")
        logger.info("Returning english srt for video_id=%s", video_id)
        return FileResponse(srt_path, filename=f"{video_id}-english.srt", media_type="application/x-subrip")

    output_path = CLIPS_DIR / f"{video_id}-english-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, 0.0, duration, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning english mp4 for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")
