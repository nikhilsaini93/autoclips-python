"""Tests: subtitles=english skips the Whisper translate pass when the audio
is already English (production request: no English->English translation).

The skip reads only the already-cached native transcript (peek, no compute),
so first-time calls keep today's single-pass behavior.
"""

import json


def _native_cache_file(tmp_path, monkeypatch, video_id="vid"):
    import app.services.video.transcribe as tr_mod
    from app.config import settings

    monkeypatch.setattr(tr_mod, "TMP_DIR", tmp_path)
    key = f"{video_id}-auto-transcribe-m{settings.WHISPER_MODEL_SIZE}-v2-words.json"
    return tmp_path / key


def test_peek_returns_none_when_nothing_cached(tmp_path, monkeypatch):
    import app.services.video.transcribe as tr_mod

    monkeypatch.setattr(tr_mod, "TMP_DIR", tmp_path)
    assert tr_mod.peek_cached_transcript("nothere", task="transcribe") is None


def test_peek_returns_cached_transcript(tmp_path, monkeypatch):
    import app.services.video.transcribe as tr_mod

    cache_file = _native_cache_file(tmp_path, monkeypatch)
    cache_file.write_text(json.dumps({
        "language": "en",
        "words": [{"start": 0.0, "end": 1.0, "text": "hello"}],
    }), encoding="utf-8")
    out = tr_mod.peek_cached_transcript("vid", task="transcribe")
    assert out["language"] == "en"
    assert out["words"][0]["text"] == "hello"
    assert isinstance(out.get("segments"), list)  # v1 upgraded


def test_english_reuses_cached_native_english(monkeypatch, tmp_path):
    import app.services.viral as viral_mod
    import app.services.video.transcribe as tr_mod

    cache_file = _native_cache_file(tmp_path, monkeypatch)
    cache_file.write_text(json.dumps({
        "language": "en",
        "words": [{"start": 0.0, "end": 1.0, "text": "hello"}],
    }), encoding="utf-8")

    def _boom(*a):
        raise AssertionError("translate pass must be skipped for English audio")

    monkeypatch.setattr(viral_mod, "transcribe_words_english", _boom)
    out = viral_mod.resolve_subtitle_words("vid", tmp_path / "v.mp4", "english")
    assert [w["text"] for w in out] == ["hello"]


def test_english_still_translates_hindi_audio(monkeypatch, tmp_path):
    import app.services.viral as viral_mod
    import app.services.video.transcribe as tr_mod

    cache_file = _native_cache_file(tmp_path, monkeypatch)
    cache_file.write_text(json.dumps({
        "language": "hi",
        "words": [{"start": 0.0, "end": 1.0, "text": "शादी"}],
    }), encoding="utf-8")

    called = {}

    def _fake_translate(*a):
        called["yes"] = True
        return [{"start": 0.0, "end": 1.0, "text": "wedding"}]

    monkeypatch.setattr(viral_mod, "transcribe_words_english", _fake_translate)
    out = viral_mod.resolve_subtitle_words("vid", tmp_path / "v.mp4", "english")
    assert called and [w["text"] for w in out] == ["wedding"]


def test_english_translates_when_no_cache(monkeypatch, tmp_path):
    import app.services.viral as viral_mod
    import app.services.video.transcribe as tr_mod

    monkeypatch.setattr(tr_mod, "TMP_DIR", tmp_path)
    called = {}

    def _fake_translate(*a):
        called["yes"] = True
        return [{"start": 0.0, "end": 1.0, "text": "hi"}]

    monkeypatch.setattr(viral_mod, "transcribe_words_english", _fake_translate)
    out = viral_mod.resolve_subtitle_words("vid", tmp_path / "v.mp4", "english")
    assert called and out[0]["text"] == "hi"
