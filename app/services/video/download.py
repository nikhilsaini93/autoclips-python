import logging
import subprocess
import time
from pathlib import Path

from app.config import DOWNLOAD_DIR, TMP_DIR, settings
from app.services.video.process import run

logger = logging.getLogger(__name__)


def _cookies_file() -> str:
    """Configured cookies file if it exists, else '' (never pass missing file)."""
    raw = (settings.YOUTUBE_COOKIES_FILE or "").strip()
    if not raw:
        return ""
    try:
        if Path(raw).expanduser().exists():
            return raw
        logger.warning("YOUTUBE_COOKIES_FILE=%s missing, downloading without cookies.", raw)
    except OSError:
        pass
    return ""


def _base_command(video_id: str) -> tuple[list, str]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    command = ["yt-dlp"]
    cookies = _cookies_file()
    if cookies:
        command += ["--cookies", cookies]
    return command, url


def download_video(video_id: str) -> Path:
    """Downloads (once) and caches the source video on disk, keyed by video_id.

    Intermittent Colab fix: retries with android -> web player clients since
    YouTube flags shared datacenter IPs randomly. Raises last error if all fail.
    """
    output_path = DOWNLOAD_DIR / f"{video_id}.mp4"
    if output_path.exists():
        logger.info("Using cached download for video_id=%s (%s)", video_id, output_path)
        return output_path

    logger.info("Downloading video_id=%s via yt-dlp...", video_id)
    t0 = time.perf_counter()
    base, url = _base_command(video_id)
    # android often bypasses bot check without cookies; web handles most formats.
    attempts = [
        ["--extractor-args", "youtube:player_client=android"],
        [],
    ]
    last_err: Exception | None = None
    for i, extra in enumerate(attempts):
        command = (
            base
            + extra
            + [
                # prefer H.264 (avc1) - decodes much faster on CPU than AV1/VP9,
                # which matters since clips get re-decoded multiple times
                # (face-detection frame grabs + the final cut/subtitle burn)
                "-f",
                "bv*[vcodec^=avc1]+ba/bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                str(output_path),
                url,
            ]
        )
        try:
            run(command)
            break
        except Exception as e:
            last_err = e
            logger.warning("Download attempt %d/2 failed for %s (%s)", i + 1, video_id, e)
            try:
                if output_path.exists():
                    output_path.unlink()
            except OSError:
                pass
            if i < len(attempts) - 1:
                time.sleep(3)
    else:
        raise last_err or RuntimeError(f"yt-dlp failed for {video_id}")
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
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = map(int, output.split(","))
    return width, height


def get_video_duration(input_path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(output)
