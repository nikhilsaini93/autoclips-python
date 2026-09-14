"""YouTube description builder (channel format)."""

FAIR_USE_DISCLAIMER = (
    "Copyright Disclaimer Under Section 107 of the Copyright Act 1976, allowance is made for "
    "'Fair Use' for purposes such as criticism, comment, news reporting, teaching, scholarship, "
    "and research. Fair use is a permitted by copyright statute that might otherwise be "
    "infringing, Non-profit, educational or personal use tips the balance in favour of fair use."
)


def build_yt_description(description=None, video_id=None, video_url=None, credit=None) -> str:
    """Builds the final Shorts description in the channel format.

    Format:
        <AI Hinglish description>

        Credit :- @channel
        Original video link:- https://www.youtube.com/watch?v=VIDEO_ID

        <Fair-use disclaimer>

    Hashtags are appended separately by build_upload_body, so they are NOT
    added here. Idempotent — if credit/link/disclaimer lines are already
    present they are not duplicated.
    """
    desc = (description or "").strip()

    link = (video_url or "").strip()
    if not link and video_id:
        link = f"https://www.youtube.com/watch?v={str(video_id).strip()}"

    credit = (credit or "").strip()

    parts: list[str] = []
    if desc:
        parts.append(desc)

    if credit and "Credit :-" not in desc:
        parts.append(f"Credit :- {credit}")
    if link and "Original video link" not in desc:
        parts.append(f"Original video link:- {link}")

    if "Copyright Disclaimer Under Section 107" not in desc:
        if parts:
            parts.append("")
        parts.append(FAIR_USE_DISCLAIMER)

    if not parts:
        return FAIR_USE_DISCLAIMER
    return "\n".join(parts).strip()
