import logging
import os
import time
import uuid
from pathlib import Path

from app.config import ROOT, TMP_DIR, settings
from app.services.video.download import get_video_dimensions
from app.services.video.faces import detect_face_center_x
from app.services.video.process import run
from app.services.video.subtitles import write_ass

logger = logging.getLogger(__name__)

FONTS_DIR = ROOT / "assets" / "fonts"


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
    vf_parts = ["setpts=PTS-STARTPTS"]

    if vertical_crop:
        # Allow skipping face detection for speed (center crop fallback)
        if settings.SKIP_FACE_DETECT:
            logger.info("SKIP_FACE_DETECT=1 — using center crop")
            face_center_x = width / 2
        else:
            face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)
        crop_width = round(height * 9 / 16)
        x = round(face_center_x - crop_width / 2)
        x = max(0, min(x, width - crop_width))
        vf_parts.append(f"crop={crop_width}:{height}:{x}:0")
        vf_parts.append("scale=1080:1920")
        vf_parts.append("setsar=1")

    srt_path: Path | None = None
    if words:
        # Poppins Bold Italic (true) + random-pop colors via ASS.
        # Style lives inside the ASS (see subtitles.write_ass); fontsdir
        # lets libass find assets/fonts/Poppins-*.
        srt_path = TMP_DIR / f"sub-{os.getpid()}-{uuid.uuid4().hex[:8]}.ass"
        has_subs = write_ass(
            words, start_sec, end_sec, srt_path,
            font=settings.SUBTITLE_FONT,
            fontsize=settings.SUBTITLE_FONTSIZE,
        )
        if has_subs:
            # ffmpeg's subtitles filter needs ':' escaped inside the path arg
            escaped_path = str(srt_path).replace("\\", "/").replace(":", "\\:")
            fontsdir = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:")
            vf_parts.append(f"subtitles='{escaped_path}':fontsdir='{fontsdir}'")

    vf = ",".join(vf_parts)
    duration_sec = end_sec - start_sec

    logger.info("Encoding with ffmpeg (duration=%.1fs)...", duration_sec)
    try:
        run([
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(input_path),
            "-t", f"{duration_sec:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-vf", vf,
            "-af", "asetpts=PTS-STARTPTS",
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-threads", "0",
            "-c:a", "aac",
            str(output_path),
        ])
    finally:
        # Always clean up the per-render ASS so tmp/ doesn't grow forever.
        try:
            if srt_path is not None and srt_path.exists():
                srt_path.unlink()
        except OSError:
            pass

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Render complete: %s (%.1f MB) in %.1fs total",
        output_path, size_mb, time.perf_counter() - t0,
    )
