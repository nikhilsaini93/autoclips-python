from pathlib import Path

import pytest

from app.services.video.ids import get_video_id
from app.services.video.subtitles import format_srt_timestamp, write_srt
from app.services.video.timeutils import time_to_seconds
from app.services.video.transcribe import words_to_transcript_text


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
