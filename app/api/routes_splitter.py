import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.deps import limiter
from app.api.schemas import SplitterPartInfo, SplitterRequest
from app.services.jobs import create_job, get_job, jobs
from app.services.splitter import (
    delete_job,
    job_dir,
    plan_parts,
    read_manifest,
    run_splitter_job,
)
from app.services.video.download import get_video_metadata
from app.services.video.ids import get_video_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/splitter/jobs", status_code=202)
@limiter.limit("10/minute")
async def create_splitter_job(
    request: Request, req: SplitterRequest, background_tasks: BackgroundTasks,
):
    """Queues a split job and returns immediately — poll GET /splitter/jobs/{id}
    for progress, then GET .../parts to list or download the generated files."""
    try:
        video_id = get_video_id(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        meta = await asyncio.to_thread(get_video_metadata, video_id)
    except RuntimeError as e:
        raise HTTPException(422, str(e))

    try:
        parts = plan_parts(meta["duration"], req.part_minutes * 60.0)
    except ValueError as e:
        raise HTTPException(400, str(e))

    job_id = create_job("splitter")
    background_tasks.add_task(
        run_splitter_job, job_id, video_id, req.part_minutes, req.aspect_ratio,
        meta.get("title"),
    )
    logger.info(
        "Splitter job %s queued: video_id=%s part_minutes=%s aspect_ratio=%s parts=%d",
        job_id, video_id, req.part_minutes, req.aspect_ratio, len(parts),
    )
    return {
        "job_id": job_id,
        "status": "running",
        "total_parts": len(parts),
        "part_minutes": req.part_minutes,
        "aspect_ratio": req.aspect_ratio,
        "video": meta,
        "poll": f"/splitter/jobs/{job_id}",
    }


def _require_manifest(job_id: str) -> dict:
    manifest = read_manifest(job_id)
    if manifest is None:
        if get_job(job_id) is None:
            raise HTTPException(404, "job not found")
        raise HTTPException(404, "job manifest not ready yet")
    return manifest


@router.get("/splitter/jobs/{job_id}")
def get_splitter_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    response = dict(job)
    manifest = read_manifest(job_id)
    if manifest is not None:
        response["manifest"] = manifest
    return response


@router.get("/splitter/jobs/{job_id}/parts", response_model=list[SplitterPartInfo])
def list_splitter_parts(job_id: str):
    manifest = _require_manifest(job_id)
    return manifest.get("parts", [])


@router.get("/splitter/jobs/{job_id}/parts/{part_number}")
def download_splitter_part(job_id: str, part_number: int):
    manifest = _require_manifest(job_id)
    entry = next(
        (p for p in manifest.get("parts", []) if p.get("part") == part_number),
        None,
    )
    if entry is None:
        raise HTTPException(404, "part not found")
    path = job_dir(job_id) / entry["filename"]
    if not path.is_file():
        raise HTTPException(404, "part file missing (job may have failed)")
    return FileResponse(path, filename=entry["filename"], media_type="video/mp4")


@router.delete("/splitter/jobs/{job_id}")
def delete_splitter_job(job_id: str):
    """Deletes the job's generated parts. The cached source video is kept
    (shared with the other workflows; purge it with Delete Files/cleanup)."""
    job = get_job(job_id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "job is still running — wait for it to finish")
    existed_in_memory = jobs.pop(job_id, None) is not None
    folder_existed = job_dir(job_id).exists()
    if not existed_in_memory and not folder_existed:
        raise HTTPException(404, "job not found")
    freed = delete_job(job_id)
    logger.info("Deleted splitter job %s (%.1f MB freed)", job_id, freed)
    return {"success": True, "job_id": job_id, "freed_mb": freed}
