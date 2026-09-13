import os

import pytest


def test_build_upload_body_unlisted_with_tags():
    from youtube_upload import build_upload_body

    body = build_upload_body("My Title", "My desc", ["shorts", "#hindi ", ""])
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["madeForKids"] is False
    assert body["snippet"]["title"] == "My Title"
    assert body["snippet"]["tags"] == ["shorts", "hindi"]
    assert "#shorts" in body["snippet"]["description"]
    assert "My desc" in body["snippet"]["description"]


def test_build_upload_body_truncates_title():
    from youtube_upload import build_upload_body

    body = build_upload_body("x" * 150, None, None)
    assert len(body["snippet"]["title"]) <= 100


def test_build_upload_body_tolerates_missing_pack():
    from youtube_upload import build_upload_body

    body = build_upload_body("", None, None)
    assert body["snippet"]["title"] == "Untitled Clip"
    assert body["snippet"]["tags"] == []


def test_upload_unlisted_missing_file(tmp_path):
    from youtube_upload import upload_unlisted

    with pytest.raises(FileNotFoundError):
        upload_unlisted(tmp_path / "nope.mp4", "T")


def test_get_youtube_service_needs_creds(monkeypatch):
    import youtube_upload as yu

    monkeypatch.delenv("YT_CLIENT_ID", raising=False)
    monkeypatch.delenv("YT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("YT_REFRESH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        yu.get_youtube_service()


def test_pending_upload_single_use(monkeypatch, tmp_path):
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    import main as m

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    token = m.register_pending_upload(clip, "vid-1", "Title", "Desc", ["a"], "123")
    assert token and len(token) <= 16  # fits Telegram callback_data limits
    entry = m.pop_pending_upload(token)
    assert entry["clip_id"] == "vid-1"
    assert entry["title"] == "Title"
    # Double-tap: token is gone.
    assert m.pop_pending_upload(token) is None
