import json
from pathlib import Path

import pytest

from video_utils import (
    cleanup_old_files,
    detect_face_center_x,
    format_srt_timestamp,
    get_video_id,
    render_video,
    time_to_seconds,
    words_to_transcript_text,
    write_srt,
)


def test_time_to_seconds():
    assert time_to_seconds("00:01:20") == 80.0
    assert time_to_seconds("01:20") == 80.0
    assert time_to_seconds(45) == 45.0
    assert time_to_seconds("90") == 90.0
    with pytest.raises(ValueError):
        time_to_seconds("bad")


def test_get_video_id_watch():
    assert get_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_get_video_id_shorts_embed():
    assert get_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert get_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert get_video_id("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"


def test_get_video_id_invalid():
    with pytest.raises(ValueError):
        get_video_id("https://www.youtube.com/watch?v=")
    with pytest.raises(ValueError):
        get_video_id("https://example.com/not-youtube")


def test_format_srt_timestamp():
    assert format_srt_timestamp(0) == "00:00:00,000"
    assert format_srt_timestamp(80.5) == "00:01:20,500"


def test_write_srt_filters_window(tmp_path: Path):
    words = [
        {"start": 70.0, "end": 71.0, "text": "hello"},
        {"start": 81.0, "end": 82.0, "text": "world"},
        {"start": 200.0, "end": 201.0, "text": "far"},
    ]
    srt = tmp_path / "out.srt"
    assert write_srt(words, 80.0, 90.0, srt) is True
    text = srt.read_text(encoding="utf-8")
    assert "world" in text
    assert "hello" not in text


def test_write_srt_empty_returns_false(tmp_path: Path):
    words = [{"start": 1.0, "end": 2.0, "text": "hi"}]
    assert write_srt(words, 80.0, 90.0, tmp_path / "empty.srt") is False


def test_words_to_transcript_text():
    words = [{"start": float(i), "end": float(i) + 0.4, "text": f"w{i}"} for i in range(25)]
    text = words_to_transcript_text(words)
    lines = text.splitlines()
    assert len(lines) == 3  # 12 + 12 + 1
    assert lines[0].startswith("[0]")


def test_find_viral_clips_parsing(monkeypatch):
    import video_utils as vu

    class FakeModels:
        def generate_content(self, model, contents):
            class Resp:
                text = '```json\n[{"title":"T","start":"00:01:20","end":"00:01:55","score":95,"reason":"hook"}]\n```'
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(vu, "get_genai_client", lambda: FakeClient())
    clips = vu.find_viral_clips("dummy transcript")
    assert len(clips) == 1
    assert clips[0]["title"] == "T"
    # Old-format replies without the YT pack get normalized defaults.
    assert clips[0]["hashtags"] == []
    assert clips[0]["description"] == ""


def test_find_viral_clips_title_pack_hindi(monkeypatch):
    import video_utils as vu

    payload = json.dumps([{
        "title": "सफलता का एक मंत्र",
        "start": "00:01:20",
        "end": "00:01:55",
        "score": 95,
        "reason": "hook",
        "hashtags": ["#shorts", "motivation", "hindi ", "", "a", "b", "c", "d"],
        "description": "  Shorts description here.  ",
    }])

    seen = {}

    class FakeModels:
        def generate_content(self, model, contents):
            seen["prompt"] = contents
            class Resp:
                text = payload
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(vu, "get_genai_client", lambda: FakeClient())
    clips = vu.find_viral_clips("नमस्ते transcript", language="hi")
    assert len(clips) == 1
    c = clips[0]
    assert c["title"] == "सफलता का एक मंत्र"
    # '#' stripped, empties dropped, capped at 6.
    assert c["hashtags"] == ["shorts", "motivation", "hindi", "a", "b", "c"]
    assert c["description"] == "Shorts description here."
    assert "hi" in seen["prompt"]


def test_find_viral_clips_unparseable_raises(monkeypatch):
    import video_utils as vu

    class FakeModels:
        def generate_content(self, model, contents):
            class Resp:
                text = "not json at all {{{"
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(vu, "get_genai_client", lambda: FakeClient())
    with pytest.raises(RuntimeError):
        vu.find_viral_clips("dummy")


def test_render_video_creates_parent_dir(tmp_path: Path, monkeypatch):
    import video_utils as vu

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "missing" / "clip.mp4"

    monkeypatch.setattr(vu, "get_video_dimensions", lambda _: (1920, 1080))

    def fake_run(cmd):
        assert out.parent.exists()
        out.write_bytes(b"video")

    monkeypatch.setattr(vu, "run", fake_run)

    vu.render_video(src, out, 1.0, 3.0, vertical_crop=False)
    assert out.parent.exists()
    assert out.exists()


def test_detect_face_center_x_does_not_use_unsupported_vsync(tmp_path: Path, monkeypatch):
    import video_utils as vu

    seen = []

    def fake_run(cmd):
        seen.append(cmd)

    monkeypatch.setattr(vu, "run", fake_run)
    monkeypatch.setattr(vu, "detect_face_center_x_in_frame", lambda _frame_path, _frame_width: 100.0)

    detect_face_center_x(tmp_path / "video.mp4", 0.0, 10.0, 200)

    assert seen
    assert all("-vsync" not in cmd for cmd in seen)


def test_cleanup_old_files(tmp_path: Path, monkeypatch):
    import video_utils as vu

    dl = tmp_path / "downloads"
    cl = tmp_path / "clips"
    tm = tmp_path / "tmp"
    dl.mkdir()
    cl.mkdir()
    tm.mkdir()
    monkeypatch.setattr(vu, "DOWNLOAD_DIR", dl)
    monkeypatch.setattr(vu, "CLIPS_DIR", cl)
    monkeypatch.setattr(vu, "TMP_DIR", tm)

    old = cl / "old.mp4"
    old.write_bytes(b"x" * 100)
    import os
    import time

    ancient = time.time() - 100 * 3600
    os.utime(old, (ancient, ancient))

    result = vu.cleanup_old_files(max_age_hours=72)
    assert result["deleted"] >= 1
    assert not old.exists()
