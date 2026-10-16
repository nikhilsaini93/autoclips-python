"""YouTube splitter: cut a video into equal-length parts and convert each part
to 9:16 (1080x1920) or 16:9 (1920x1080) with the full source frame centered and
black padding where needed.

Self-contained workflow: no face detection, no subtitles, no AI analysis. All
artifacts live under storage/splitter/{job_id}/ (part_XX.mp4 + manifest.json)
so nothing overlaps with existing downloads/clips/transcripts.
"""

import json
import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.config import SPLITTER_DIR, settings
from app.services.jobs import update_job
from app.services.video.download import (
    download_video,
    get_video_dimensions,
    get_video_duration,
)
from app.services.video.process import run

logger = logging.getLogger(__name__)

# scale=...force_original_aspect_ratio=decrease keeps the WHOLE source frame;
# force_divisible_by=2 avoids odd intermediate sizes x264 dislikes; pad centers
# it on the black canvas; setsar=1 keeps players happy.
ASPECT_FILTERS = {
    "9:16": (
        "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    ),
    "16:9": (
        "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    ),
}

ASPECT_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080)}


def aspect_filter(aspect_ratio: str) -> str:
    try:
        return ASPECT_FILTERS[aspect_ratio]
    except KeyError:
        raise ValueError(
            f"Unknown aspect ratio {aspect_ratio!r} (expected '9:16' or '16:9')"
        )


def output_size(aspect_ratio: str) -> tuple[int, int]:
    try:
        return ASPECT_SIZES[aspect_ratio]
    except KeyError:
        raise ValueError(
            f"Unknown aspect ratio {aspect_ratio!r} (expected '9:16' or '16:9')"
        )


def plan_parts(duration_sec: float, part_seconds: float) -> list[tuple[float, float]]:
    """Splits [0, duration] into contiguous, non-overlapping parts.

    total_parts = ceil(duration / part); the final part carries the remainder
    (may be shorter than the selected length). Boundary times tile the video
    exactly: part[i].end == part[i+1].start, no frame duplicated or omitted.
    """
    try:
        duration_sec = float(duration_sec)
        part_seconds = float(part_seconds)
    except (TypeError, ValueError):
        raise ValueError("Video duration and part length must be numbers")
    if duration_sec <= 0:
        raise ValueError("Video duration is unknown or zero (live stream?)")
    if part_seconds <= 0:
        raise ValueError("Part duration must be a positive number")

    # The epsilon absorbs float error so exact multiples stay exact
    # (600s / 120s must be 5 parts, not 6).
    total_parts = max(1, math.ceil(duration_sec / part_seconds - 1e-9))
    parts: list[tuple[float, float]] = []
    for i in range(total_parts):
        start = i * part_seconds
        end = min(start + part_seconds, duration_sec)
        if end - start <= 0:
            break
        parts.append((start, end))
    if not parts:
        raise ValueError("Video is too short to split")
    return parts


def parse_part_minutes(text: str) -> float:
    """Parses a Telegram minutes input ("2", "1.5") into seconds."""
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("Send the part length in minutes, e.g. 2")
    try:
        minutes = float(raw)
    except ValueError:
        raise ValueError("Send a number of minutes, e.g. 2 or 1.5")
    if minutes <= 0:
        raise ValueError("Part length must be greater than 0 minutes")
    return minutes * 60.0


def part_filename(number: int, total_parts: int) -> str:
    """part_01.mp4 … part_09.mp4; widens automatically for 100+ parts."""
    width = max(2, len(str(total_parts)))
    return f"part_{number:0{width}d}.mp4"


def build_part_command(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    duration_sec: float,
    aspect_ratio: str,
    has_audio: bool = True,
) -> list:
    """ffmpeg command for one part: accurate cut + letterbox/pillarbox canvas.

    Always re-encodes (never stream-copies) so segment boundaries are
    frame-accurate and every part has identical output dimensions.
    """
    command = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", str(input_path),
        "-t", f"{duration_sec:.3f}",
        "-map", "0:v:0",
    ]
    if has_audio:
        command += ["-map", "0:a:0?"]
    command += [
        "-vf", aspect_filter(aspect_ratio),
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-threads", "0",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        command += ["-af", "asetpts=PTS-STARTPTS", "-c:a", "aac"]
    command.append(str(output_path))
    return command


def _has_audio_stream(input_path: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(input_path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return bool(out)
    except Exception:
        logger.warning("Could not probe audio for %s — assuming audio present", input_path)
        return True


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def job_dir(job_id: str) -> Path:
    return SPLITTER_DIR / job_id


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "manifest.json"


def write_manifest(job_id: str, manifest: dict) -> None:
    """Atomic write so API readers never see a half-written manifest."""
    path = manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_manifest(job_id: str) -> dict | None:
    path = manifest_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Corrupt splitter manifest for job %s", job_id)
        return None


def delete_job(job_id: str) -> float:
    """Deletes the job folder, returns freed MB.

    The cached source video in storage/downloads is intentionally kept — it is
    shared with the other workflows and purged by Delete Files/cleanup.
    """
    folder = job_dir(job_id)
    freed = 0
    if folder.exists():
        freed = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        shutil.rmtree(folder)
    return round(freed / (1024 * 1024), 1)


def _ensure_disk_space(video_path: Path) -> None:
    """Fails fast when free disk clearly cannot hold source + output parts."""
    try:
        source_mb = video_path.stat().st_size / (1024 * 1024)
    except OSError:
        return
    required_mb = 2 * source_mb + settings.SPLITTER_MIN_DISK_FREE_MB
    try:
        free_mb = shutil.disk_usage(str(SPLITTER_DIR.parent)).free / (1024 * 1024)
    except OSError:
        return
    if free_mb < required_mb:
        raise RuntimeError(
            f"Not enough disk space: {free_mb:.0f} MB free, need ~{required_mb:.0f} MB "
            f"(source {source_mb:.0f} MB x2 + {settings.SPLITTER_MIN_DISK_FREE_MB} MB margin)"
        )


def run_splitter_job(
    job_id: str,
    video_id: str,
    part_minutes: float,
    aspect_ratio: str,
    title: str | None = None,
) -> dict:
    """Full pipeline: download -> plan -> encode parts -> manifest.

    Never raises: failures are recorded in the job entry and manifest so the
    API status endpoint and Telegram progress message can report them.
    Returns the final manifest.
    """
    started = time.time()
    part_seconds = float(part_minutes) * 60.0
    width, height = output_size(aspect_ratio)
    manifest: dict = {
        "job_id": job_id,
        "created_at": started,
        "status": "running",
        "source": {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title or "",
        },
        "part_minutes": part_minutes,
        "aspect_ratio": aspect_ratio,
        "output": {"width": width, "height": height},
        "parts": [],
        "error": None,
    }

    try:
        update_job(job_id, stage="downloading", percent=2, progress="downloading")
        video_path = download_video(video_id)
        duration = get_video_duration(video_path)
        source_w, source_h = get_video_dimensions(video_path)
        manifest["source"].update({
            "duration_seconds": round(duration, 3),
            "width": source_w,
            "height": source_h,
        })

        parts_plan = plan_parts(duration, part_seconds)
        total = len(parts_plan)
        _ensure_disk_space(video_path)

        out_dir = job_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest["parts"] = [
            {
                "part": i + 1,
                "filename": part_filename(i + 1, total),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "status": "pending",
                "size_mb": None,
            }
            for i, (start, end) in enumerate(parts_plan)
        ]
        write_manifest(job_id, manifest)
        update_job(job_id, total_clips=total, done_clips=0)
        logger.info(
            "Splitter job %s: video_id=%s duration=%.1fs part=%.1fmin ratio=%s parts=%d",
            job_id, video_id, duration, part_minutes, aspect_ratio, total,
        )

        has_audio = _has_audio_stream(video_path)

        for i, (start, end) in enumerate(parts_plan):
            entry = manifest["parts"][i]
            target = out_dir / entry["filename"]
            tmp_target = out_dir / f"{entry['filename']}.tmp.mp4"
            update_job(
                job_id, stage="splitting", done_clips=i,
                percent=8 + int(85 * i / total),
                progress=f"splitting {i + 1}/{total}",
            )
            try:
                run(build_part_command(
                    video_path, tmp_target, start, end - start, aspect_ratio, has_audio,
                ))
                os.replace(tmp_target, target)
            except subprocess.CalledProcessError as e:
                _safe_unlink(tmp_target)
                entry["status"] = "failed"
                write_manifest(job_id, manifest)
                raise RuntimeError(f"ffmpeg failed while encoding {entry['filename']}") from e

            entry["status"] = "done"
            entry["size_mb"] = round(target.stat().st_size / (1024 * 1024), 1)
            write_manifest(job_id, manifest)
            update_job(
                job_id, done_clips=i + 1,
                percent=8 + int(85 * (i + 1) / total),
                progress=f"part {i + 1}/{total} completed",
            )
            logger.info("Splitter job %s: %s done (%.1f MB)", job_id, entry["filename"], entry["size_mb"])

        manifest["status"] = "done"
        manifest["elapsed_seconds"] = round(time.time() - started, 1)
        write_manifest(job_id, manifest)
        update_job(
            job_id, status="done", stage="done", percent=100, done_clips=total,
            result={"parts": total, "job_dir": str(out_dir)},
        )
        logger.info("Splitter job %s finished: %d part(s) in %.1fs", job_id, total, time.time() - started)
        return manifest
    except Exception as e:
        logger.exception("Splitter job %s failed", job_id)
        manifest["status"] = "failed"
        manifest["error"] = str(e)
        try:
            write_manifest(job_id, manifest)
        except OSError:
            pass
        for leftover in job_dir(job_id).glob("*.tmp.mp4"):
            _safe_unlink(leftover)
        update_job(job_id, status="failed", stage="failed", error=str(e))
        return manifest
