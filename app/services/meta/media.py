"""Media preflight helpers for Meta Reels (duration via ffprobe).

Never raises for probe failures — returns None so callers can skip the
duration check and let Meta validate server-side.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Facebook Page Reels: 3–90s (error 1363128 otherwise).
FB_REEL_MIN_SECONDS = 3.0
FB_REEL_MAX_SECONDS = 90.0
# Instagram Reels: allow up to ~3min via API (longer is rejected server-side).
IG_REEL_MIN_SECONDS = 3.0
IG_REEL_MAX_SECONDS = 180.0


def get_media_duration(video_path) -> float | None:
    """Returns duration in seconds via ffprobe, or None if unavailable."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("ffprobe not found — skipping duration preflight")
        return None
    except Exception:
        logger.warning("ffprobe duration check failed for %s", video_path, exc_info=True)
        return None
    try:
        return float((out.stdout or "").strip())
    except (TypeError, ValueError):
        logger.warning("Could not parse ffprobe duration for %s: %r", video_path, out.stdout)
        return None


def check_fb_reel_eligibility(duration: float | None) -> tuple[bool, str]:
    """Hard gate for Facebook Reels (3–90s). None duration → allow (server validates)."""
    if duration is None:
        return True, ""
    if duration < FB_REEL_MIN_SECONDS:
        return False, f"clip is {duration:.1f}s — Facebook Reels need >= 3s"
    if duration > FB_REEL_MAX_SECONDS:
        return False, f"clip is {duration:.1f}s — Facebook Reels max is 90s"
    return True, ""


def check_ig_reel_eligibility(duration: float | None) -> tuple[bool, str]:
    """Soft gate for Instagram Reels. Over-long clips warn, not fail."""
    if duration is None:
        return True, ""
    if duration < IG_REEL_MIN_SECONDS:
        return False, f"clip is {duration:.1f}s — Instagram Reels need >= 3s"
    if duration > IG_REEL_MAX_SECONDS:
        return False, f"clip is {duration:.1f}s — over ~3min, Instagram may reject it"
    return True, ""


def media_size_mb(video_path) -> float:
    try:
        return Path(video_path).stat().st_size / (1024 * 1024)
    except OSError:
        return 0.0
