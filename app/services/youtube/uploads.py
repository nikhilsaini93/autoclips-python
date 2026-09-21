"""YouTube Data API v3 uploads (Unlisted) for approved Telegram clips.

Auth is OAuth user credentials (service accounts cannot upload to YouTube):
  YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN in .env
Get the refresh token once via:  python scripts/get_youtube_token.py

One upload costs ~1600 quota units; default project quota (10,000/day)
allows roughly 6 uploads per day.
"""

import logging
from pathlib import Path

from app.config import settings
from app.services.youtube.descriptions import build_yt_description

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"

TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


def _clean_tags(hashtags) -> list[str]:
    tags: list[str] = []
    for t in hashtags or []:
        tag = str(t).strip("# ").strip()
        if not tag:
            continue
        tag = tag.replace(" ", "")
        if tag and tag not in tags:
            tags.append(tag)
    # YouTube tags: ~500 chars total including commas.
    total = 0
    kept: list[str] = []
    for tag in tags:
        total += len(tag) + 1
        if total > 480:
            break
        kept.append(tag)
    return kept


def build_upload_body(title, description=None, hashtags=None, category_id=None, video_id=None, video_url=None, credit=None) -> dict:
    """Builds the videos.insert request body. Pure function — easy to test."""
    clean_title = (title or "Untitled Clip").strip() or "Untitled Clip"
    if len(clean_title) > TITLE_LIMIT:
        clean_title = clean_title[: TITLE_LIMIT - 1] + "…"

    tags = _clean_tags(hashtags)
    desc = build_yt_description(description, video_id=video_id, video_url=video_url, credit=credit)
    if tags:
        tag_line = " ".join(f"#{t}" for t in tags)
        if tag_line not in desc:
            desc = f"{desc}\n\n{tag_line}".strip() if desc else tag_line
    if len(desc) > DESCRIPTION_LIMIT:
        desc = desc[: DESCRIPTION_LIMIT - 1] + "…"

    return {
        "snippet": {
            "title": clean_title,
            "description": desc,
            "tags": tags,
            "categoryId": category_id or settings.YT_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": "unlisted",
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
        },
    }


def get_youtube_service(client_id=None, client_secret=None, refresh_token=None):
    """Builds an authenticated YouTube service, refreshing the token."""
    cid = client_id or settings.YT_CLIENT_ID
    csec = client_secret or settings.YT_CLIENT_SECRET
    rtoken = refresh_token or settings.YT_REFRESH_TOKEN
    if not (cid and csec and rtoken):
        raise RuntimeError(
            "YouTube OAuth not configured. Set YT_CLIENT_ID / YT_CLIENT_SECRET / "
            "YT_REFRESH_TOKEN in .env (see docs + scripts/get_youtube_token.py)."
        )
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=rtoken,
        token_uri=_TOKEN_URI,
        client_id=cid,
        client_secret=csec,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_unlisted(video_path, title, description=None, hashtags=None, category_id=None, video_id=None, video_url=None, credit=None) -> dict:
    """Uploads a file as Unlisted. Returns {"video_id": ..., "url": ...}."""
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Clip not found: {path}")

    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    size_mb = path.stat().st_size / (1024 * 1024)
    logger.info("Uploading %s (%.1f MB) to YouTube as unlisted...", path.name, size_mb)

    service = get_youtube_service()
    body = build_upload_body(title, description, hashtags, category_id, video_id=video_id, video_url=video_url, credit=credit)
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)

    try:
        response = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    except HttpError as e:
        content = getattr(e, "content", b"").decode(errors="ignore")[-1000:]
        if "quotaExceeded" in content:
            raise RuntimeError("YouTube quota exceeded (≈6 uploads/day on default quota). Try again tomorrow.")
        if "invalid_grant" in content or "unauthorized" in content.lower():
            raise RuntimeError("YouTube OAuth token invalid/expired. Re-run scripts/get_youtube_token.py and update YT_REFRESH_TOKEN.")
        raise RuntimeError(f"YouTube upload failed: {content or e}")

    new_video_id = response.get("id", "")
    logger.info("YouTube upload complete: video_id=%s title=%r", new_video_id, body["snippet"]["title"])
    return {"video_id": new_video_id, "url": f"https://youtu.be/{new_video_id}" if new_video_id else ""}
