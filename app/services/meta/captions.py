"""Caption/description builders for Instagram Reels + Facebook Reels.

Pure functions — easy to unit test. The credit / original-link / fair-use
footer is shared with YouTube (single channel format) via
app.services.youtube.descriptions.build_yt_description.
"""

import logging

from app.services.youtube.descriptions import build_yt_description

logger = logging.getLogger(__name__)

IG_CAPTION_LIMIT = 2200
IG_HASHTAG_LIMIT = 30
FB_DESCRIPTION_LIMIT = 5000
FB_TITLE_LIMIT = 255


def clean_tags(hashtags, limit: int = IG_HASHTAG_LIMIT) -> list[str]:
    """Normalizes hashtag inputs: strips '#'/spaces, removes inner spaces,
    dedupes (case-insensitive), drops empties, caps at `limit`."""
    tags: list[str] = []
    seen: set[str] = set()
    for t in hashtags or []:
        tag = str(t).strip("# ").strip()
        if not tag:
            continue
        tag = tag.replace(" ", "")
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def _hashtag_line(tags: list[str]) -> str:
    return " ".join(f"#{t}" for t in tags)


def build_ig_caption(
    title=None,
    description=None,
    hashtags=None,
    video_id=None,
    video_url=None,
    credit=None,
) -> str:
    """Builds the Instagram Reels caption.

    Layout:
        <title>
        <blank>
        <description + Credit + Original link + fair-use footer>
        <blank>
        <#tags>

    Truncated to IG_CAPTION_LIMIT (2200) chars. Hashtags capped at 30.
    """
    clean_title = (title or "Untitled Clip").strip() or "Untitled Clip"
    tags = clean_tags(hashtags, IG_HASHTAG_LIMIT)
    body = build_yt_description(description, video_id=video_id, video_url=video_url, credit=credit)

    parts: list[str] = [clean_title]
    if body:
        parts.append("")
        parts.append(body)
    if tags:
        parts.append("")
        parts.append(_hashtag_line(tags))
    caption = "\n".join(parts).strip()

    if len(caption) > IG_CAPTION_LIMIT:
        # Keep title + footer intact when possible: cut the middle body first.
        # Simple approach: hard-truncate with ellipsis (title is first line).
        caption = caption[: IG_CAPTION_LIMIT - 1] + "…"
        logger.debug("IG caption truncated to %d chars", IG_CAPTION_LIMIT)
    return caption


def build_fb_title(title=None) -> str:
    clean = (title or "Untitled Clip").strip() or "Untitled Clip"
    if len(clean) > FB_TITLE_LIMIT:
        clean = clean[: FB_TITLE_LIMIT - 1] + "…"
    return clean


def build_fb_description(
    description=None,
    hashtags=None,
    video_id=None,
    video_url=None,
    credit=None,
) -> str:
    """Builds the Facebook Reel/video description (footer + hashtags)."""
    tags = clean_tags(hashtags, IG_HASHTAG_LIMIT)
    desc = build_yt_description(description, video_id=video_id, video_url=video_url, credit=credit)
    if tags:
        tag_line = _hashtag_line(tags)
        if tag_line not in desc:
            desc = f"{desc}\n\n{tag_line}".strip() if desc else tag_line
    if len(desc) > FB_DESCRIPTION_LIMIT:
        desc = desc[: FB_DESCRIPTION_LIMIT - 1] + "…"
    return desc
