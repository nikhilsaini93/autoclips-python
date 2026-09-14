"""Meta token/config helpers. No network calls here (see scripts/get_meta_token.py)."""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

SETUP_HINT = (
    "Meta upload not configured. Set META_PAGE_TOKEN + FB_PAGE_ID (Facebook) "
    "and IG_USER_ID (Instagram) in .env — see docs/META_SETUP.md and "
    "scripts/get_meta_token.py."
)


def mask_token(token: str) -> str:
    if not token:
        return "<empty>"
    t = str(token)
    if len(t) <= 8:
        return "****"
    return f"{t[:4]}…{t[-4:]}"


def get_page_token() -> str:
    token = (settings.META_PAGE_TOKEN or "").strip()
    if not token:
        raise RuntimeError(SETUP_HINT)
    return token


def is_facebook_configured() -> bool:
    return bool((settings.META_PAGE_TOKEN or "").strip() and (settings.FB_PAGE_ID or "").strip())


def is_instagram_configured() -> bool:
    return bool((settings.META_PAGE_TOKEN or "").strip() and (settings.IG_USER_ID or "").strip())


def is_meta_configured() -> bool:
    return is_facebook_configured() or is_instagram_configured()


def require_facebook() -> tuple[str, str]:
    token = (settings.META_PAGE_TOKEN or "").strip()
    page_id = (settings.FB_PAGE_ID or "").strip()
    if not (token and page_id):
        raise RuntimeError(
            "Facebook upload not configured. Set META_PAGE_TOKEN + FB_PAGE_ID "
            "in .env (see docs/META_SETUP.md)."
        )
    return token, page_id


def require_instagram() -> tuple[str, str]:
    token = (settings.META_PAGE_TOKEN or "").strip()
    ig_id = (settings.IG_USER_ID or "").strip()
    if not (token and ig_id):
        raise RuntimeError(
            "Instagram upload not configured. Set META_PAGE_TOKEN + IG_USER_ID "
            "in .env (see docs/META_SETUP.md). The IG account must be a "
            "Professional account linked to your Facebook Page."
        )
    return token, ig_id


def api_version() -> str:
    return (settings.META_API_VERSION or "v26.0").strip() or "v26.0"
