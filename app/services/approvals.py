import uuid

# Pending upload approvals (per-clip Approve/Deny buttons, YouTube + Meta).
# token -> {"path": str, "clip_id": str, "title": str,
#           "description": str, "hashtags": list, "chat_id": str,
#           "source_video_id": str, "credit": str, "platform": str}
# One clip gets up to 3 tokens (yt / ig / fb) pointing at the same file so
# each platform can be approved (and retried) independently.
# In-memory like `jobs`: a restart expires pending approvals.
pending_uploads: dict = {}


def register_pending_upload(path, clip_id, title, description, hashtags, chat_id, source_video_id=None, credit=None, platform="youtube") -> str:
    token = uuid.uuid4().hex[:8]
    pending_uploads[token] = {
        "path": str(path),
        "clip_id": clip_id,
        "title": title or "Untitled Clip",
        "description": description or "",
        "hashtags": list(hashtags or []),
        "chat_id": str(chat_id) if chat_id is not None else "",
        "source_video_id": source_video_id or "",
        "credit": credit or "",
        "platform": platform or "youtube",
    }
    return token


def pop_pending_upload(token: str):
    return pending_uploads.pop(token, None)


def drop_sibling_uploads(path, keep_token: str | None = None) -> int:
    """Removes all pending tokens pointing at `path` (except keep_token).

    Used on Deny so stale Approve buttons for other platforms can't point
    at the deleted file. Returns the number of tokens dropped.
    """
    target = str(path)
    doomed = [
        tok for tok, entry in list(pending_uploads.items())
        if str(entry.get("path")) == target and tok != keep_token
    ]
    for tok in doomed:
        pending_uploads.pop(tok, None)
    return len(doomed)
