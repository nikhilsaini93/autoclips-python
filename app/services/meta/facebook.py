"""Facebook Page Reels upload via Video API (resumable, local file).

Flow:
  1. POST /{page-id}/video_reels?upload_phase=start → video_id
  2. POST rupload.facebook.com/video-upload/{v}/{video-id} (binary, offset=0)
  3. Poll GET /{video-id}?fields=status until ready
  4. POST /{page-id}/video_reels?upload_phase=finish&video_id=...&title=...&description=...

Long clips (>90s) optionally fall back to a regular Page video post
(POST graph-video.facebook.com/{v}/{page-id}/videos multipart).

Rate limit: ~30 API Reels / 24h on POST /{page-id}/video_reels.
"""

import logging
import time
from pathlib import Path

from app.config import settings
from app.services.meta import captions as meta_captions
from app.services.meta.media import (
    check_fb_reel_eligibility,
    get_media_duration,
    media_size_mb,
)
from app.services.meta.tokens import api_version, mask_token, require_facebook

logger = logging.getLogger(__name__)


def is_facebook_configured() -> bool:
    from app.services.meta.tokens import is_facebook_configured as _c

    return _c()


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{api_version()}/{path.lstrip('/')}"


def _graph_video(path: str) -> str:
    return f"https://graph-video.facebook.com/{api_version()}/{path.lstrip('/')}"


def _friendly_error(action: str, resp_json, status: int) -> RuntimeError:
    err = {}
    if isinstance(resp_json, dict):
        err = resp_json.get("error") or {}
    msg = str(err.get("message") or resp_json or f"HTTP {status}").strip()
    code = err.get("code")
    sub = err.get("error_subcode")
    suffix = f" (code={code}, subcode={sub})" if code else ""
    low = msg.lower()
    if status in (401, 403) or code in (190, 200, 102):
        return RuntimeError(
            f"Facebook {action} failed: invalid/expired token{suffix}. "
            f"Re-run scripts/get_meta_token.py and update META_PAGE_TOKEN. Detail: {msg[:300]}"
        )
    if "permission" in low or code == 200:
        return RuntimeError(
            f"Facebook {action} failed: missing permissions{suffix}. Need "
            f"pages_show_list + pages_read_engagement + pages_manage_posts and "
            f"CREATE_CONTENT task on the Page. Detail: {msg[:300]}"
        )
    if "limit" in low or "rate" in low or code in (368, 613, 80004):
        return RuntimeError(
            f"Facebook {action} failed: rate limit hit{suffix} (~30 Reels/24h). "
            f"Wait before retrying. Detail: {msg[:300]}"
        )
    if "duration" in low or code == 1363128:
        return RuntimeError(
            f"Facebook {action} failed: duration not supported{suffix} "
            f"(Reels need 3–90s). Detail: {msg[:300]}"
        )
    if "resolution" in low or "frame" in low or str(code).startswith("1363"):
        return RuntimeError(
            f"Facebook {action} failed: video specs rejected{suffix}. "
            f"Need 540x960+ (rec. 1080x1920), 24–60fps, H.264. Detail: {msg[:300]}"
        )
    return RuntimeError(f"Facebook {action} failed{suffix}: {msg[:500]}")


def start_reel_session(token: str, page_id: str) -> str:
    import requests

    url = _graph(f"{page_id}/video_reels")
    logger.info("FB: starting reel upload session...")
    try:
        resp = requests.post(
            url,
            data={"upload_phase": "start", "access_token": token},
            timeout=60,
        )
    except Exception as e:
        raise RuntimeError(f"Facebook session start failed (network): {e}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or "video_id" not in payload:
        raise _friendly_error("session start", payload, resp.status_code)
    video_id = str(payload["video_id"])
    logger.info("FB: session started video_id=%s", video_id)
    return video_id


def upload_binary(video_id: str, video_path, token: str) -> None:
    import requests

    path = Path(video_path)
    size = path.stat().st_size
    url = f"https://rupload.facebook.com/video-upload/{api_version()}/{video_id}"
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
    }
    logger.info("FB: uploading %s (%.1f MB) to video %s...", path.name, size / 1024 / 1024, video_id)
    try:
        with open(path, "rb") as fh:
            # file_url alternative (public CDN) not used — local binary only.
            resp = requests.post(url, headers=headers, data=fh, timeout=300, params={"access_token": token})
    except Exception as e:
        raise RuntimeError(f"Facebook binary upload failed (network): {e}")
    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:500]}
        raise _friendly_error("video upload", payload, resp.status_code)
    logger.info("FB: binary upload accepted for video %s", video_id)


def wait_video_ready(video_id: str, token: str, timeout: int = 180, interval: int = 5) -> None:
    import requests

    url = _graph(f"{video_id}")
    deadline = time.time() + timeout
    logger.info("FB: waiting for video %s to be ready...", video_id)
    while True:
        try:
            resp = requests.get(url, params={"fields": "status", "access_token": token}, timeout=30)
        except Exception as e:
            raise RuntimeError(f"Facebook status check failed (network): {e}")
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400:
            raise _friendly_error("status check", payload, resp.status_code)
        status = payload.get("status") or {}
        # status shape varies: {"video_status": "ready"} or plain string.
        state = str(status.get("video_status") if isinstance(status, dict) else status or "").lower()
        if state in ("ready", "complete", "completed", "published", "ok"):
            logger.info("FB: video %s ready (%s)", video_id, state)
            return
        if state in ("error", "failed"):
            raise RuntimeError(f"Facebook video {video_id} processing failed ({state}).")
        if not state:
            # No status field yet/ever — treat upload-accepted as ready after
            # a short grace, rather than hanging until timeout.
            logger.debug("FB: no video status returned, assuming ready")
            return
        if time.time() >= deadline:
            raise RuntimeError(
                f"Facebook video {video_id} still {state} after {timeout}s — "
                "try finishing the publish anyway or re-upload."
            )
        time.sleep(interval)


def finish_reel(
    page_id: str,
    video_id: str,
    token: str,
    title: str,
    description: str,
    video_state: str = "PUBLISHED",
) -> dict:
    import requests

    url = _graph(f"{page_id}/video_reels")
    logger.info("FB: finishing reel video %s (state=%s)...", video_id, video_state)
    try:
        resp = requests.post(
            url,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "title": title or "",
                "description": description or "",
                "video_state": video_state,
                "access_token": token,
            },
            timeout=60,
        )
    except Exception as e:
        raise RuntimeError(f"Facebook reel publish failed (network): {e}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or not payload.get("success", True):
        # Some versions return {"success": true, "post_id": ...} or just ids.
        if resp.status_code >= 400:
            raise _friendly_error("reel publish", payload, resp.status_code)
    logger.info("FB: reel published video_id=%s resp=%s", video_id, str(payload)[:200])
    post_id = str(payload.get("post_id") or payload.get("id") or video_id)
    return {"video_id": video_id, "post_id": post_id, "url": f"https://www.facebook.com/{post_id}" if post_id else ""}


def publish_regular_video(
    page_id: str, video_path, token: str, title: str, description: str
) -> dict:
    """Fallback for >90s clips: regular Page video post (multipart upload)."""
    import requests

    path = Path(video_path)
    url = _graph_video(f"{page_id}/videos")
    logger.info("FB: publishing regular video %s (%.1f MB)...", path.name, media_size_mb(path))
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                url,
                data={"title": title or "", "description": description or "", "access_token": token},
                files={"file": (path.name, fh, "video/mp4")},
                timeout=300,
            )
    except Exception as e:
        raise RuntimeError(f"Facebook video publish failed (network): {e}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or "id" not in payload:
        raise _friendly_error("video publish", payload, resp.status_code)
    vid = str(payload["id"])
    logger.info("FB: regular video published id=%s", vid)
    return {"video_id": vid, "post_id": vid, "url": f"https://www.facebook.com/{vid}"}


def upload_facebook_reel(
    video_path,
    title=None,
    description=None,
    hashtags=None,
    video_id=None,
    video_url=None,
    credit=None,
    video_state: str = "PUBLISHED",
    allow_fallback_to_video: bool | None = None,
    token: str | None = None,
    page_id: str | None = None,
) -> dict:
    """Uploads a local MP4 as a Facebook Page Reel (or regular video fallback).

    Returns {video_id, post_id, url, kind} where kind is 'reel' or 'video'.
    """
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Clip not found: {path}")
    tok, pid = (token, page_id) if (token and page_id) else require_facebook()
    if allow_fallback_to_video is None:
        allow_fallback_to_video = bool(settings.META_FB_FALLBACK_TO_VIDEO)

    fb_title = meta_captions.build_fb_title(title)
    fb_desc = meta_captions.build_fb_description(
        description, hashtags, video_id=video_id, video_url=video_url, credit=credit
    )

    duration = get_media_duration(path)
    ok, reason = check_fb_reel_eligibility(duration)
    if not ok:
        logger.warning("FB preflight: %s", reason)
        if duration is not None and duration > 90.0 and allow_fallback_to_video:
            logger.info("FB: clip >90s — falling back to regular Page video post")
            out = publish_regular_video(pid, path, tok, fb_title, fb_desc)
            return {**out, "kind": "video", "fallback": True, "reason": reason}
        if duration is not None and duration > 90.0:
            raise RuntimeError(
                f"{reason}. Set META_FB_FALLBACK_TO_VIDEO=true to post it as a "
                "regular Page video instead."
            )
        raise RuntimeError(f"Facebook Reels need 3–90s ({reason}).")

    logger.info(
        "FB upload %s (%.1f MB, token=%s) title=%r",
        path.name, media_size_mb(path), mask_token(tok), fb_title[:60],
    )
    vid = start_reel_session(tok, pid)
    upload_binary(vid, path, tok)
    wait_video_ready(vid, tok)
    out = finish_reel(pid, vid, tok, fb_title, fb_desc, video_state=video_state)
    return {**out, "kind": "reel", "fallback": False}
