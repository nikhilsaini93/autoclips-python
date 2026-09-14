import pytest


def test_build_upload_body_unlisted_with_tags():
    from app.services.youtube.uploads import build_upload_body

    body = build_upload_body("My Title", "My desc", ["shorts", "#hindi ", ""])
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["madeForKids"] is False
    assert body["snippet"]["title"] == "My Title"
    assert body["snippet"]["tags"] == ["shorts", "hindi"]
    assert "#shorts" in body["snippet"]["description"]
    assert "My desc" in body["snippet"]["description"]


def test_build_upload_body_truncates_title():
    from app.services.youtube.uploads import build_upload_body

    body = build_upload_body("x" * 150, None, None)
    assert len(body["snippet"]["title"]) <= 100


def test_build_upload_body_tolerates_missing_pack():
    from app.services.youtube.uploads import build_upload_body

    body = build_upload_body("", None, None)
    assert body["snippet"]["title"] == "Untitled Clip"
    assert body["snippet"]["tags"] == []


def test_upload_unlisted_missing_file(tmp_path):
    from app.services.youtube.uploads import upload_unlisted

    with pytest.raises(FileNotFoundError):
        upload_unlisted(tmp_path / "nope.mp4", "T")


def test_get_youtube_service_needs_creds(monkeypatch):
    from app.config import settings
    import app.services.youtube.uploads as uploads

    monkeypatch.setattr(settings, "YT_CLIENT_ID", "")
    monkeypatch.setattr(settings, "YT_CLIENT_SECRET", "")
    monkeypatch.setattr(settings, "YT_REFRESH_TOKEN", "")
    with pytest.raises(RuntimeError, match="not configured"):
        uploads.get_youtube_service()
