import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from app.config import TMP_DIR, settings
from app.services.video.download import get_video_dimensions
from app.services.video.faces import detect_face_center_x
from app.services.video.fonts import warn_if_glyphs_missing
from app.services.video.process import run
from app.services.video.subtitles import write_srt

logger = logging.getLogger(__name__)


def target_video_maxrate_kbps(duration_sec: float, audio_kbps: int = 128) -> int:
    """Video bitrate budget so muxed (video+audio) stays under Telegram cap.

    est_MB = (video_kbps + audio_kbps) * duration / 8 / 1024 <= TELEGRAM_MAX_VIDEO_MB
    15% safety margin for container overhead, clamped to sane H.264 range.
    """
    cap_mb = float(getattr(settings, "TELEGRAM_MAX_VIDEO_MB", 50.0) or 50.0)
    duration_sec = max(1.0, float(duration_sec or 1.0))
    budget = (cap_mb * 8192.0 / duration_sec - float(audio_kbps)) * 0.85
    return int(max(500, min(2500, round(budget))))


def render_timeout_sec(duration_sec: float) -> float:
    """ffmpeg budget for one clip encode.

    Auto mode (RENDER_TIMEOUT_SEC=0): max(600s, 30x duration) — loaded CPUs
    need ~10x realtime for 1080x1920 software x264 + libass + loudnorm, and
    viral batches run 2 encodes in parallel. Explicit RENDER_TIMEOUT_SEC
    overrides (<=0 also means auto).
    """
    try:
        override = float(getattr(settings, "RENDER_TIMEOUT_SEC", 0) or 0)
    except (TypeError, ValueError):
        override = 0.0
    if override > 0:
        return override
    try:
        duration_sec = max(1.0, float(duration_sec or 1.0))
    except (TypeError, ValueError):
        duration_sec = 1.0
    return max(600.0, duration_sec * 30.0)


def is_over_telegram_limit(path: Path) -> bool:
    """True when file exceeds the configured Telegram sendVideo cap."""
    try:
        cap_mb = float(getattr(settings, "TELEGRAM_MAX_VIDEO_MB", 50.0) or 50.0)
        size_mb = Path(path).stat().st_size / (1024 * 1024)
        return size_mb > cap_mb
    except OSError:
        return False


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def compress_for_telegram(path: Path) -> bool:
    """Re-compress an oversize clip in place to fit the Telegram cap.

    Downscales to 720x1280 + tighter bitrate budget. Returns True when the
    result now fits (or when size can't be determined but re-encode ran).
    """
    try:
        duration = _probe_duration(path) or 60.0
        maxrate = target_video_maxrate_kbps(duration)
        bufsize = maxrate * 2
        tmp = Path(str(path) + ".recompress.mp4")
        run([
            "ffmpeg", "-y",
            "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=720:1280:flags=lanczos,setsar=1,fps=30",
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-crf", "24",
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{bufsize}k",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-level", "4.0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-threads", "0",
            str(tmp),
        ])
        try:
            os.replace(tmp, path)
        except OSError:
            return False
        return not is_over_telegram_limit(path)
    except Exception:
        logger.exception("Telegram recompress failed for %s", path)
        return False


def _even(n: int) -> int:
    n = int(n)
    return n - (n % 2)


def render_video(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    words: list | None = None,
    vertical_crop: bool = True,
) -> None:
    logger.info(
        "Rendering %s -> %s [%.1f, %.1f] vertical_crop=%s subtitles=%s",
        input_path, output_path, start_sec, end_sec, vertical_crop, bool(words),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    width, height = get_video_dimensions(input_path)
    # ffprobe can report rotated phone video without applying the rotate tag —
    # guard against inverted dims breaking crop math downstream.
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        width, height = 1920, 1080
    vf_parts = ["setpts=PTS-STARTPTS"]

    if vertical_crop:
        # Allow skipping face detection for speed (center crop fallback)
        if settings.SKIP_FACE_DETECT:
            logger.info("SKIP_FACE_DETECT=1 — using center crop")
            face_center_x = width / 2
        else:
            face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)
        try:
            face_center_x = float(face_center_x)
        except (TypeError, ValueError):
            face_center_x = width / 2
        # Clamp crop to source so narrow (<9:16) sources don't fail with
        # crop>width; keep even for yuv420p.
        crop_width = _even(min(width, round(height * 9 / 16)))
        crop_width = max(64, crop_width)
        crop_h = _even(height)
        x = _even(round(face_center_x - crop_width / 2))
        x = max(0, min(x, max(0, width - crop_width)))
        # Only crop/scale when needed — already-vertical 1080x1920 skips the
        # lossy 2.66x upscale path.
        needs_crop = not (crop_width == width and crop_h == height)
        needs_scale = not (crop_width == 1080 and crop_h == 1920)
        if needs_crop:
            vf_parts.append(f"crop={crop_width}:{crop_h}:{x}:0")
        if needs_scale:
            # Don't upscale tiny crops beyond 1080p width — cap at source
            # quality instead of inventing pixels; lanczos for sharp faces.
            if crop_width < 540:
                vf_parts.append("scale=720:1280:flags=lanczos")
            else:
                vf_parts.append("scale=1080:1920:flags=lanczos")
            vf_parts.append("setsar=1")
        vf_parts.append(f"fps={int(getattr(settings, 'RENDER_FPS', 30) or 30)}")

    srt_path: Path | None = None
    if words:
        # Hindi captions + no Devanagari font = blank burn-in. Warn now
        # (with the clip timestamps) instead of shipping a silent video.
        warn_if_glyphs_missing(words)
        srt_path = TMP_DIR / f"sub-{os.getpid()}-{uuid.uuid4().hex[:8]}.srt"
        has_subs = write_srt(words, start_sec, end_sec, srt_path)
        if has_subs:
            # ffmpeg's subtitles filter needs ':' escaped inside the path arg
            escaped_path = str(srt_path).replace("\\", "/").replace(":", "\\:")
            # Viral style: large bold + heavy stroke/box + safe-area margin
            # so captions survive bright backgrounds and TikTok/Reels UI.
            style = (
                "FontName=DejaVu Sans,FontSize=26,Bold=1,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H80000000,BackColour=&H80000000,BorderStyle=4,Outline=3,Shadow=0,"
                "Alignment=2,MarginV=280"
            )
            vf_parts.append(f"subtitles='{escaped_path}':force_style='{style}'")

    vf = ",".join(vf_parts)
    duration_sec = max(0.1, end_sec - start_sec)
    maxrate = target_video_maxrate_kbps(duration_sec)
    bufsize = maxrate * 2

    logger.info("Encoding with ffmpeg (duration=%.1fs, maxrate=%dk)...", duration_sec, maxrate)
    try:
        # Accurate seek (-i BEFORE -ss): keyframe seek (-ss before -i) froze
        # the first ~0.5s and desynced burned subs. Slower but hook-accurate.
        # Timeout scales with duration (slow CPUs need ~10x realtime).
        run([
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ss", f"{float(start_sec):.3f}",
            "-t", f"{duration_sec:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", vf,
            "-af", "asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000:async=1",
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-crf", str(settings.RENDER_CRF),
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{bufsize}k",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-level", "4.0",
            "-threads", "0",
            "-c:a", "aac",
            "-b:a", str(getattr(settings, "RENDER_AUDIO_BITRATE", "128k") or "128k"),
            "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-shortest",
            str(output_path),
        ], timeout=render_timeout_sec(duration_sec))
    finally:
        # Always clean up the per-render SRT so tmp/ doesn't grow forever.
        try:
            if srt_path is not None and srt_path.exists():
                srt_path.unlink()
        except OSError:
            pass

    try:
        size_mb = output_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    logger.info(
        "Render complete: %s (%.1f MB) in %.1fs total",
        output_path, size_mb, time.perf_counter() - t0,
    )
    # Telegram hard-caps sendVideo (~50MB): auto-recompress instead of
    # shipping an unsendable file.
    try:
        if is_over_telegram_limit(output_path):
            logger.warning("Output %.1f MB over Telegram cap — recompressing %s", size_mb, output_path)
            compress_for_telegram(output_path)
    except Exception:
        logger.exception("Post-render Telegram size check failed for %s", output_path)
