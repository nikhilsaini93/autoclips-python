from fastapi import APIRouter, Request

from app.api.deps import limiter
from app.api.schemas import ViralClipsRequest
from app.services.viral import generate_viral_clips

router = APIRouter()


@router.post("/clips/viral")
@limiter.limit("5/minute")
async def clips_viral(request: Request, req: ViralClipsRequest):
    """Auto-find viral clips, render them, send them to Telegram, and return metadata."""

    video_id, entries = await generate_viral_clips(req)

    return {
        "success": True,
        "clips_sent": sum(1 for entry in entries if entry.get("sent")),
        "clips": [
            {
                "clip_id": f"{video_id}-{i}",
                "file": entry["path"].name,
                "title": entry["title"],
                "score": entry["score"],
                "reason": entry["reason"],
                "hashtags": entry.get("hashtags") or [],
                "description": entry.get("description"),
                "sent": bool(entry.get("sent")),
            }
            for i, entry in enumerate(entries, start=1)
        ],
    }
