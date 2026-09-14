"""Meta (Instagram + Facebook) uploads for approved Telegram clips."""

from app.services.meta.captions import build_fb_description, build_fb_title, build_ig_caption
from app.services.meta.facebook import is_facebook_configured, upload_facebook_reel
from app.services.meta.instagram import is_instagram_configured, upload_instagram_reel
from app.services.meta.tokens import (
    is_meta_configured,
    require_facebook,
    require_instagram,
)

__all__ = [
    "build_fb_description",
    "build_fb_title",
    "build_ig_caption",
    "is_facebook_configured",
    "is_instagram_configured",
    "is_meta_configured",
    "require_facebook",
    "require_instagram",
    "upload_facebook_reel",
    "upload_instagram_reel",
]
