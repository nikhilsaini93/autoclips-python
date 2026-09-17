"""T4 pipeline tests: VAD transcript shape, silence snapper, face smoothing."""

from app.services.video.timeutils import seconds_to_time, snap_to_silence
from app.services.video.transcribe import (
    _ensure_v2_shape,
    compute_silences,
    words_to_segments,
    words_to_transcript_text,
)


def _words():
    # 4 words with a 0.8s pause between word2 and word3.
    return [
        {"start": 10.0, "end": 10.4, "text": "hello"},
        {"start": 10.5, "end": 10.9, "text": "world."},
        {"start": 11.7, "end": 12.1, "text": "next"},
        {"start": 12.2, "end": 12.6, "text": "thought."},
    ]


def test_compute_silences_finds_pause():
    sil = compute_silences(_words(), min_gap_sec=0.4)
    assert len(sil) == 1
    assert sil[0]["start"] == 10.9
    assert sil[0]["end"] == 11.7


def test_ensure_v2_shape_upgrades_v1_cache():
    v1 = {"language": "hi", "words": _words()}
    v2 = _ensure_v2_shape(v1)
    assert isinstance(v2["segments"], list) and v2["segments"]
    assert isinstance(v2["silences"], list) and len(v2["silences"]) == 1
    # Idempotent on real v2 dicts.
    assert _ensure_v2_shape(v2) is v2


def test_words_to_segments_splits_on_sentence():
    segs = words_to_segments(_words())
    assert len(segs) == 2
    assert segs[0]["text"] == "hello world."
    assert segs[1]["text"] == "next thought."


def test_words_to_transcript_text_structured():
    words = _words()
    text = words_to_transcript_text(words, segments=words_to_segments(words),
                                    silences=compute_silences(words))
    assert "[10.0-10.9] hello world." in text
    assert "[pause 0.8s]" in text
    assert "[11.7-12.6] next thought." in text


def test_words_to_transcript_text_legacy_fallback():
    words = [{"start": float(i), "end": float(i) + 0.4, "text": f"w{i}"} for i in range(25)]
    lines = words_to_transcript_text(words).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("[0]")


def test_snap_moves_boundary_into_silence():
    words = _words()
    sil = compute_silences(words)
    segs = words_to_segments(words)
    # Raw end sits mid-pause; snapper should center it in the silence.
    ns, ne = snap_to_silence(10.0, 11.0, words=words, silences=sil,
                             segments=segs, window_sec=0.8)
    assert ns == 10.0
    assert abs(ne - 11.3) < 1e-6


def test_snap_never_inverts_range():
    ns, ne = snap_to_silence(12.0, 12.1, words=[], silences=[], segments=[],
                             window_sec=0.8)
    assert (ns, ne) == (12.0, 12.1)


def test_snap_clamps_to_duration():
    ns, ne = snap_to_silence(0.0, 100.0, words=[], silences=[], segments=[],
                             window_sec=0.8, duration_sec=60.0)
    assert ne == 60.0
    assert ns == 0.0


def test_seconds_to_time_format():
    assert seconds_to_time(80.5) == "00:01:20.500"
    assert seconds_to_time(-3) == "00:00:00.000"


def test_smooth_centers_moving_average():
    from app.services.video.faces import smooth_centers

    assert smooth_centers([10.0, 20.0, 30.0], window=2) == [10.0, 15.0, 25.0]
    assert smooth_centers([], window=3) == []


def test_detector_kind_falls_back(monkeypatch):
    import app.services.video.faces as faces

    monkeypatch.setattr(faces.settings, "FACE_MODEL", "bogus")
    assert faces.detector_kind() == "res10"


def test_sample_times_dense_and_clamped(monkeypatch):
    import app.services.video.faces as faces

    monkeypatch.setattr(faces.settings, "FACE_SAMPLE_FPS", 1.0)
    times = faces._sample_times(0.0, 10.0)
    assert len(times) == 10
    assert times[0] > 0.0 and times[-1] < 10.0
    # Long video clamps to 60 grabs.
    assert len(faces._sample_times(0.0, 600.0)) == 60


def test_detect_face_center_x_falls_back_to_center(tmp_path, monkeypatch):
    import app.services.video.faces as faces

    monkeypatch.setattr(faces.settings, "FACE_SAMPLE_FPS", 1.0)
    monkeypatch.setattr(faces, "run", lambda cmd: None)
    monkeypatch.setattr(faces, "detect_face_center_x_in_frame", lambda fp, fw: None)
    assert faces.detect_face_center_x(tmp_path / "v.mp4", 0.0, 4.0, 200) == 100.0


def test_srt_timestamp_no_overflow():
    from app.services.video.subtitles import chunk_words_for_captions, format_srt_timestamp

    assert format_srt_timestamp(80.9999) == "00:01:21,000"
    words = [
        {"start": 0.0, "end": 0.3, "text": "hello"},
        {"start": 0.4, "end": 0.7, "text": "world."},
        {"start": 0.8, "end": 1.1, "text": "next"},
    ]
    chunks = chunk_words_for_captions(words)
    assert chunks[0][2] == "hello world."
    assert chunks[1][2] == "next"
