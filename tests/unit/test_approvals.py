import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")


def test_pending_upload_single_use(tmp_path):
    from app.services.approvals import pop_pending_upload, register_pending_upload

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    token = register_pending_upload(clip, "vid-1", "Title", "Desc", ["a"], "123")
    assert token and len(token) <= 16  # fits Telegram callback_data limits
    entry = pop_pending_upload(token)
    assert entry["clip_id"] == "vid-1"
    assert entry["title"] == "Title"
    # Double-tap: token is gone.
    assert pop_pending_upload(token) is None
