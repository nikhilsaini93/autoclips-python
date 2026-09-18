import logging
import shutil as _shutil
import subprocess as _sp

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import limiter
from app.config import settings
from app.services.jobs import jobs
from app.services.video.cleanup import cleanup_old_files

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health():
    try:
        _sp.run(["ffmpeg", "-version"], capture_output=True, check=True)
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False
    try:
        _sp.run(["ffprobe", "-version"], capture_output=True, check=True)
        ffprobe_ok = True
    except Exception:
        ffprobe_ok = False
    try:
        total, used, free = _shutil.disk_usage(".")
        disk_free_mb = round(free / (1024 * 1024), 1)
    except Exception:
        disk_free_mb = None
    return {
        "status": "ok",
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
        "gemini_key_set": bool(settings.GEMINI_API_KEY),
        "disk_free_mb": disk_free_mb,
        "whisper_model": settings.WHISPER_MODEL_SIZE,
        "youtube_configured": bool(
            settings.YT_CLIENT_ID and settings.YT_CLIENT_SECRET and settings.YT_REFRESH_TOKEN
        ),
        "instagram_configured": bool(settings.META_PAGE_TOKEN and settings.IG_USER_ID),
        "facebook_configured": bool(settings.META_PAGE_TOKEN and settings.FB_PAGE_ID),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.post("/admin/cleanup")
@limiter.limit("10/minute")
def admin_cleanup(request: Request, max_age_hours: float = 72, max_total_mb: float | None = None, token: str | None = None):
    """Delete files older than max_age_hours in storage/downloads|clips|tmp|videos."""
    if settings.ADMIN_TOKEN and token != settings.ADMIN_TOKEN:
        raise HTTPException(401, "invalid admin token")
    result = cleanup_old_files(max_age_hours=max_age_hours, max_total_mb=max_total_mb)
    return {"success": True, **result}
