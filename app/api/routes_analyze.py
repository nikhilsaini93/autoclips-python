import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import limiter
from app.api.schemas import AnalyzeClipsRequest, ViralClipInfo
from app.services.video.timeutils import time_to_seconds
from app.services.viral import analyze_viral_clips, prepare

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/clips/analyze", response_model=List[ViralClipInfo])
@limiter.limit("10/minute")
def clips_analyze(request: Request, req: AnalyzeClipsRequest):
    """AI-only analysis pass: no rendering, no video files produced. The
    model watches the transcript, decides for itself how many clips are
    worth cutting (anywhere from 1 to 10+), and returns each one's
    timestamps, a title, a confidence score, and its reasoning - so you can
    review the picks before spending time actually rendering any of them."""
    logger.info("clip analysis requested url=%s max_clips=%s", req.url, req.max_clips)
    video_id, video_path = prepare(req.url)
    candidates = analyze_viral_clips(video_id, video_path, req.max_clips)

    results = []
    for i, c in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(c["start"])
            end_sec = time_to_seconds(c["end"])
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed candidate #%d (%s): %s", i, c, e)
            continue
        if end_sec <= start_sec:
            logger.warning("Skipping candidate #%d with end<=start: %s", i, c)
            continue
        results.append(ViralClipInfo(
            title=c.get("title"),
            start=c["start"],
            end=c["end"],
            duration_seconds=round(end_sec - start_sec, 1),
            score=c.get("score"),
            reason=c.get("reason"),
            hashtags=c.get("hashtags") or [],
            description=c.get("description"),
        ))

    if not results:
        raise HTTPException(422, "All candidate clips were malformed")

    logger.info("Analysis found %d viral-worthy clip(s) for video_id=%s", len(results), video_id)
    return results
