import time
import uuid

# In-memory job tracker (no Redis per scope).
# Telegram long generations create entries here; clients poll GET /jobs/{id}.
jobs: dict = {}


def create_job(kind: str) -> str:
    now = time.time()
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "progress": "",
        "stage": "",
        "percent": 0,
        "total_clips": 0,
        "done_clips": 0,
        "detail": "",
        "result": None,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    return job_id


def update_job(job_id: str, **fields):
    if job_id in jobs:
        fields.setdefault("updated_at", time.time())
        jobs[job_id].update(fields)


def get_job(job_id: str):
    return jobs.get(job_id)
