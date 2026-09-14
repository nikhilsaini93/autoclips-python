import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("no json")
        return self._payload


def _clip(tmp_path, nbytes=100):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * nbytes)
    return p


def test_tokens_require_helpers(monkeypatch):
    from app.config import settings
    import app.services.meta.tokens as tokens

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "")
    monkeypatch.setattr(settings, "FB_PAGE_ID", "")
    monkeypatch.setattr(settings, "IG_USER_ID", "")
    assert tokens.is_facebook_configured() is False
    assert tokens.is_instagram_configured() is False
    try:
        tokens.require_facebook()
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "META_PAGE_TOKEN" in str(e) or "Facebook" in str(e)
    try:
        tokens.require_instagram()
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "Instagram" in str(e)

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "FB_PAGE_ID", "page1")
    monkeypatch.setattr(settings, "IG_USER_ID", "ig1")
    assert tokens.is_facebook_configured() is True
    assert tokens.is_instagram_configured() is True


def test_drop_sibling_uploads(tmp_path):
    from app.services import approvals
    from app.services.approvals import drop_sibling_uploads, register_pending_upload

    approvals.pending_uploads.clear()
    clip = _clip(tmp_path)
    t1 = register_pending_upload(clip, "c1", "T", "D", [], "123", platform="youtube")
    t2 = register_pending_upload(clip, "c1", "T", "D", [], "123", platform="instagram")
    t3 = register_pending_upload(clip, "c1", "T", "D", [], "123", platform="facebook")
    dropped = drop_sibling_uploads(str(clip), keep_token=t1)
    assert dropped == 2
    assert t1 in approvals.pending_uploads
    assert t2 not in approvals.pending_uploads
    approvals.pending_uploads.clear()


def test_instagram_upload_reel_happy_path(monkeypatch, tmp_path):
    import app.services.meta.instagram as ig
    from app.config import settings

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "IG_USER_ID", "ig1")
    monkeypatch.setattr(ig, "get_media_duration", lambda p: 20.0)

    import requests

    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/ig1/media"):
            return FakeResp({"id": "container123"})
        if "rupload.facebook.com" in url:
            return FakeResp({"success": True})
        if url.endswith("/ig1/media_publish"):
            return FakeResp({"id": "media999"})
        raise AssertionError(url)

    def fake_get(url, params=None, timeout=None):
        if params and params.get("fields") == "status_code":
            return FakeResp({"status_code": "FINISHED"})
        return FakeResp({"permalink": "https://instagram.com/p/xyz"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    out = ig.upload_instagram_reel(_clip(tmp_path), "Title", "Desc", ["a"], video_id="v1")
    assert out["media_id"] == "media999"
    assert out["container_id"] == "container123"
    assert "instagram.com" in out["url"]


def test_instagram_token_error_maps_to_setup_hint(monkeypatch, tmp_path):
    import app.services.meta.instagram as ig
    from app.config import settings

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "IG_USER_ID", "ig1")
    monkeypatch.setattr(ig, "get_media_duration", lambda p: 20.0)

    import requests

    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResp({"error": {"message": "Invalid OAuth access token", "code": 190}}, status=401)

    monkeypatch.setattr(requests, "post", fake_post)
    try:
        ig.upload_instagram_reel(_clip(tmp_path), "T")
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "get_meta_token" in str(e)


def test_facebook_upload_reel_happy_path(monkeypatch, tmp_path):
    import app.services.meta.facebook as fb
    from app.config import settings

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "FB_PAGE_ID", "page1")
    monkeypatch.setattr(fb, "get_media_duration", lambda p: 20.0)

    import requests

    def fake_post(url, data=None, headers=None, timeout=None, params=None, files=None):
        if url.endswith("/page1/video_reels") and (data or {}).get("upload_phase") == "start":
            return FakeResp({"video_id": "vid123"})
        if "rupload.facebook.com" in url:
            return FakeResp({"success": True})
        if url.endswith("/page1/video_reels") and (data or {}).get("upload_phase") == "finish":
            return FakeResp({"success": True, "post_id": "post456"})
        raise AssertionError(url)

    def fake_get(url, params=None, timeout=None):
        return FakeResp({"status": {"video_status": "ready"}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    out = fb.upload_facebook_reel(_clip(tmp_path), "Title", "Desc", ["a"])
    assert out["kind"] == "reel"
    assert out["post_id"] == "post456"
    assert out["fallback"] is False


def test_facebook_long_clip_falls_back_to_video(monkeypatch, tmp_path):
    import app.services.meta.facebook as fb
    from app.config import settings

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "FB_PAGE_ID", "page1")
    monkeypatch.setattr(fb, "get_media_duration", lambda p: 120.0)

    called = {}

    def fake_publish(page_id, video_path, token, title, description):
        called["yes"] = True
        return {"video_id": "v9", "post_id": "v9", "url": "https://www.facebook.com/v9"}

    monkeypatch.setattr(fb, "publish_regular_video", fake_publish)
    out = fb.upload_facebook_reel(_clip(tmp_path), "T", "D", [], allow_fallback_to_video=True)
    assert out["kind"] == "video" and out["fallback"] is True and called.get("yes")


def test_facebook_long_clip_fails_without_fallback(monkeypatch, tmp_path):
    import app.services.meta.facebook as fb
    from app.config import settings

    monkeypatch.setattr(settings, "META_PAGE_TOKEN", "tok")
    monkeypatch.setattr(settings, "FB_PAGE_ID", "page1")
    monkeypatch.setattr(fb, "get_media_duration", lambda p: 120.0)
    try:
        fb.upload_facebook_reel(_clip(tmp_path), "T", allow_fallback_to_video=False)
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "90s" in str(e)


def test_upload_reel_missing_file(tmp_path):
    from app.services.meta.facebook import upload_facebook_reel
    from app.services.meta.instagram import upload_instagram_reel

    import pytest

    with pytest.raises(FileNotFoundError):
        upload_instagram_reel(tmp_path / "nope.mp4", "T")
    with pytest.raises(FileNotFoundError):
        upload_facebook_reel(tmp_path / "nope.mp4", "T")
