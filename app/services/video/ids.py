import logging
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_VIDEO_ID_RE = None


def _video_id_regex():
    global _VIDEO_ID_RE
    if _VIDEO_ID_RE is None:
        import re
        _VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
    return _VIDEO_ID_RE


def _validate_video_id(video_id: str, url: str) -> str:
    if not video_id or not _video_id_regex().match(video_id):
        logger.warning("Could not parse a video id out of URL: %s", url)
        raise ValueError("Invalid YouTube URL")
    return video_id


def get_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        # e.g. https://youtu.be/VIDEO_ID?t=10 — path holds the id
        video_id = parsed.path.lstrip("/").split("/")[0].split("?")[0].strip()
        return _validate_video_id(video_id, url)
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return _validate_video_id(qs["v"][0].strip(), url)
        # Support /shorts/ID, /embed/ID, /live/ID, /v/ID
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            return _validate_video_id(parts[1].split("?")[0].strip(), url)
    logger.warning("Could not parse a video id out of URL: %s", url)
    raise ValueError("Invalid YouTube URL")
