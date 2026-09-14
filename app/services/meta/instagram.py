"""Instagram Reels upload via Instagram Graph API (resumable, local file).

Flow (no public URL needed):
  1. POST /{ig-id}/media?media_type=REELS&upload_type=resumable → container id
  2. POST rupload.facebook.com/ig-api-upload/{v}/{container-id} (binary)
  3. Poll GET /{container-id}?fields=status_code until FINISHED
  4. POST /{ig-id}/media_publish?creation_id={container-id} → media id

Docs: developers.facebook.com — Instagram Content Publishing / resumable uploads.
Requires: IG Professional account linked to FB Page, Page access token with
instagram_basic + instagram_content_publish (+ pages_* scopes).
"""

import logging
import time
from pathlib import Path

from app.config import settings
from app.services.meta import captions as meta_captions
from app.services.meta.media import check_ig_reel_eligibility, get_media_duration, media_size_mb
from app.services.meta.tokens import api_version, mask_token, require_instagram

logger = logging.getLogger(__name__)


def is_instagram_configured() -> bool:
    from app.services.meta.tokens import is_instagram_configured as _c

    return _c()


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{api_version()}/{path.lstrip('/')}"


def _rupload(container_id: str) -> str:
    return f"https://rupload.facebook.com/ig-api-upload/{api_version()}/{container_id}"


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
            f"Instagram {action} failed: invalid/expired token{suffix}. "
            f"Re-run scripts/get_meta_token.py and update META_PAGE_TOKEN. Detail: {msg[:300]}"
        )
    if "permission" in low or "authorized" in low or code == 200:
        return RuntimeError(
            f"Instagram {action} failed: missing permissions{suffix}. Grant "
            f"instagram_basic + instagram_content_publish (+ pages_*). Detail: {msg[:300]}"
        )
    if "limit" in low or "rate" in low or code in (368, 613, 80004):
        return RuntimeError(
            f"Instagram {action} failed: rate limit hit{suffix}. "
            f"Wait before retrying. Detail: {msg[:300]}"
        )
    return RuntimeError(f"Instagram {action} failed{suffix}: {msg[:500]}")


def create_reel_container(caption: str, token: str, ig_user_id: str, share_to_feed: bool = True) -> str:
    import requests

    url = _graph(f"{ig_user_id}/media")
    data = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption or "",
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": token,
    }
    logger.info("IG: creating reel container (caption %d chars)...", len(caption or ""))
    try:
        resp = requests.post(url, data=data, timeout=60)
    except Exception as e:
        raise RuntimeError(f"Instagram container creation failed (network): {e}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or "id" not in payload:
        raise _friendly_error("container creation", payload, resp.status_code)
    container_id = str(payload["id"])
    logger.info("IG: container created id=%s", container_id)
    return container_id


def upload_binary(container_id: str, video_path, token: str) -> None:
    import requests

    path = Path(video_path)
    size = path.stat().st_size
    url = _rupload(container_id)
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
    }
    logger.info("IG: uploading %s (%.1f MB) to container %s...", path.name, size / 1024 / 1024, container_id)
    try:
        with open(path, "rb") as fh:
            resp = requests.post(url, headers=headers, data=fh, timeout=300)
    except Exception as e:
        raise RuntimeError(f"Instagram binary upload failed (network): {e}")
    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:500]}
        raise _friendly_error("video upload", payload, resp.status_code)
    logger.info("IG: binary upload accepted for container %s", container_id)


def wait_container_ready(container_id: str, token: str, timeout: int = 180, interval: int = 5) -> None:
    import requests

    url = _graph(f"{container_id}")
    deadline = time.time() + timeout
    logger.info("IG: waiting for container %s to finish processing...", container_id)
    while True:
        try:
            resp = requests.get(url, params={"fields": "status_code", "access_token": token}, timeout=30)
        except Exception as e:
            raise RuntimeError(f"Instagram status check failed (network): {e}")
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400:
            raise _friendly_error("status check", payload, resp.status_code)
        status = str(payload.get("status_code") or "").upper()
        if status == "FINISHED":
            logger.info("IG: container %s FINISHED", container_id)
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                f"Instagram container {container_id} processing {status.lower()}. "
                "The video may violate Reels specs (codec/resolution/duration). "
                "Clips are 1080x1920 H.264 — try a shorter clip."
            )
        if time.time() >= deadline:
            raise RuntimeError(
                f"Instagram container {container_id} still {status or 'processing'} "
                f"after {timeout}s — try publishing again later or re-upload."
            )
        time.sleep(interval)


def publish_container(container_id: str, token: str, ig_user_id: str) -> str:
    import requests

    url = _graph(f"{ig_user_id}/media_publish")
    logger.info("IG: publishing container %s...", container_id)
    try:
        resp = requests.post(
            url,
            data={"creation_id": container_id, "access_token": token},
            timeout=60,
        )
    except Exception as e:
        raise RuntimeError(f"Instagram publish failed (network): {e}")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or "id" not in payload:
        raise _friendly_error("publish", payload, resp.status_code)
    media_id = str(payload["id"])
    logger.info("IG: published media_id=%s", media_id)
    return media_id


def fetch_permalink(media_id: str, token: str) -> str:
    import requests

    try:
        resp = requests.get(
            _graph(f"{media_id}"),
            params={"fields": "permalink", "access_token": token},
            timeout=30,
        )
        payload = resp.json()
        link = str(payload.get("permalink") or "")
        return link
    except Exception:
        logger.debug("IG permalink fetch failed for %s", media_id, exc_info=True)
        return ""


def upload_instagram_reel(
    video_path,
    title=None,
    description=None,
    hashtags=None,
    video_id=None,
    video_url=None,
    credit=None,
    share_to_feed: bool | None = None,
    token: str | None = None,
    ig_user_id: str | None = None,
) -> dict:
    """Uploads a local MP4 as an Instagram Reel. Returns {media_id, container_id, url}."""
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Clip not found: {path}")
    tok, igid = (token, ig_user_id) if (token and ig_user_id) else require_instagram()
    if share_to_feed is None:
        share_to_feed = bool(settings.META_IG_SHARE_TO_FEED)

    duration = get_media_duration(path)
    ok, warn = check_ig_reel_eligibility(duration)
    if not ok:
        logger.warning("IG preflight: %s", warn)
        if duration is not None and duration < 3.0:
            raise RuntimeError(f"Instagram Reels need >= 3s ({warn}).")

    caption = meta_captions.build_ig_caption(
        title, description, hashtags, video_id=video_id, video_url=video_url, credit=credit
    )
    logger.info(
        "IG upload %s (%.1f MB, token=%s) caption=%d chars share_to_feed=%s",
        path.name, media_size_mb(path), mask_token(tok), len(caption), share_to_feed,
    )
    container_id = create_reel_container(caption, tok, igid, share_to_feed=share_to_feed)
    upload_binary(container_id, path, tok)
    wait_container_ready(container_id, tok)
    media_id = publish_container(container_id, tok, igid)
    url = fetch_permalink(media_id, tok)
    return {"media_id": media_id, "container_id": container_id, "url": url}
