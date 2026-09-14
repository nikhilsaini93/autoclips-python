import uuid

# Pending YouTube-upload approvals (per-clip Approve/Deny buttons).
# token -> {"path": str, "clip_id": str, "title": str,
#           "description": str, "hashtags": list, "chat_id": str,
#           "source_video_id": str, "credit": str}
# In-memory like `jobs`: a restart expires pending approvals.
pending_uploads: dict = {}


def register_pending_upload(path, clip_id, title, description, hashtags, chat_id, source_video_id=None, credit=None) -> str:
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
    }
    return token


def pop_pending_upload(token: str):
    return pending_uploads.pop(token, None)
