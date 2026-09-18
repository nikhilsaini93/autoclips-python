import logging
import subprocess
import time
from pathlib import Path

from app.config import DOWNLOAD_DIR, TMP_DIR, settings
from app.services.video.process import run

logger = logging.getLogger(__name__)


def _cookies_file() -> str:
    return settings.YOUTUBE_COOKIES_FILE or ""


def download_video(video_id: str) -> Path:
    """Downloads (once) and caches the source video on disk, keyed by video_id."""
    output_path = DOWNLOAD_DIR / f"{video_id}.mp4"
    if output_path.exists():
        try:
            # Corrupt/partial downloads (killed container, 0-byte file) were
            # reused forever — validate size + stream before trusting cache.
            if output_path.stat().st_size > 1024 * 1024:
                get_video_dimensions(output_path)
                logger.info("Using cached download for video_id=%s (%s)", video_id, output_path)
                return output_path
            logger.warning("Cached download %s too small, re-downloading", output_path)
            try:
                output_path.unlink()
            except OSError:
                pass
        except Exception:
            logger.warning("Cached download %s unreadable, re-downloading", output_path)
            try:
                output_path.unlink()
            except OSError:
                pass

    logger.info("Downloading video_id=%s via yt-dlp...", video_id)
    t0 = time.perf_counter()
    url = f"https://www.youtube.com/watch?v={video_id}"
    command = ["yt-dlp"]
    cookies = _cookies_file()
    if cookies:
        command += ["--cookies", cookies]
    command += [
        # Best quality up to 1080p with H.264 preference for fast CPU decode,
        # but allow VP9/AV1 fallback instead of capping to avc1-only 720p.
        # Height floor keeps the 9:16 upscale from amplifying a 360p source.
        "-f", "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/bv*+ba/b",
        "--format-sort", "res:1080,fps,vcodec:avc1,acodec:aac",
        "--merge-output-format", "mp4",
        "--continue", "--retries", "3",
        "-o", str(output_path),
        url,
    ]
    run(command)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Download finished for video_id=%s in %.1fs (%.1f MB)",
        video_id, time.perf_counter() - t0, size_mb,
    )
    return output_path


def get_video_credit(video_id: str) -> str:
    """Returns '@handle' credit for the source video (e.g. '@ranveerallahbadia').

    Uses yt-dlp metadata only (no download), cached under storage/tmp/ so one video
    costs one lookup. Returns '' on any failure — callers must tolerate that.
    """
    try:
        cache_path = TMP_DIR / f"{video_id}.credit.txt"
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        url = f"https://www.youtube.com/watch?v={video_id}"
        command = ["yt-dlp", "--skip-download", "--no-warnings",
                   "--print", "%(uploader_id)s||%(channel)s||%(uploader)s||%(channel_url)s"]
        cookies = _cookies_file()
        if cookies:
            command += ["--cookies", cookies]
        command.append(url)
        out = subprocess.run(command, capture_output=True, text=True, timeout=25)
        raw = (out.stdout or "").strip().splitlines()
        line = raw[-1].strip() if raw else ""
        parts = [p.strip() for p in line.split("||")]
        credit = ""
        for p in parts:
            if p and p.lower() != "na" and p.startswith("@"):
                credit = p
                break
        if not credit:
            # Fallback: build a handle from channel/uploader name.
            for p in parts:
                if p and p.lower() != "na":
                    # channel_url like https://www.youtube.com/@handle → keep handle
                    if "youtube.com/@" in p:
                        credit = "@" + p.split("/@")[-1].split("/")[0].strip()
                        break
            if not credit:
                for p in parts:
                    if p and p.lower() != "na":
                        credit = p if p.startswith("@") else f"@{p.replace(' ', '')}"
                        break
        if credit:
            try:
                cache_path.write_text(credit, encoding="utf-8")
            except OSError:
                pass
            return credit
    except Exception:
        logger.debug("Could not fetch credit for video_id=%s", video_id, exc_info=True)
    return ""


def get_video_dimensions(input_path: Path):
    try:
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(input_path)],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        width, height = map(int, output.split(","))
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid dimensions: {output}")
        return width, height
    except Exception as e:
        # Portrait phone video with a rotate tag reports swapped dims —
        # try honoring rotation before giving up so crop math stays correct.
        try:
            output = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,side_data_list",
                 "-of", "default=noprint_wrappers=1", str(input_path)],
                capture_output=True, text=True, check=True, timeout=15,
            ).stdout
            import re
            wm = re.search(r"width=(\d+)", output)
            hm = re.search(r"height=(\d+)", output)
            if wm and hm:
                w, h = int(wm.group(1)), int(hm.group(1))
                if "rotation" in output.lower() and abs(w - h) > 0:
                    # 90/270 deg rotation swaps display dims.
                    return h, w
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        raise RuntimeError(f"ffprobe dimensions failed for {input_path}: {e}")


def get_video_duration(input_path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(output)
