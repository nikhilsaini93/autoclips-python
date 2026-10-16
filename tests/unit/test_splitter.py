import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import splitter
from app.services.splitter import (
    aspect_filter,
    build_part_command,
    delete_job,
    job_dir,
    manifest_path,
    output_size,
    parse_part_minutes,
    part_filename,
    plan_parts,
    read_manifest,
    run_splitter_job,
    write_manifest,
)


def test_plan_parts_exact_multiples():
    parts = plan_parts(600, 120)
    assert len(parts) == 5
    assert parts[0] == (0.0, 120.0)
    assert parts[-1] == (480.0, 600.0)
    assert len(plan_parts(600, 60)) == 10
    assert len(plan_parts(960, 120)) == 8


def test_plan_parts_remainder_last_part():
    parts = plan_parts(630, 120)
    assert len(parts) == 6
    assert parts[-1] == (600.0, 630.0)
    assert parts[-1][1] - parts[-1][0] == 30.0


def test_plan_parts_tiles_without_gaps_or_overlap():
    duration = 1000.0
    parts = plan_parts(duration, 150.0)
    assert parts[0][0] == 0.0
    assert parts[-1][1] == duration
    for (_, end), (next_start, _) in zip(parts, parts[1:]):
        assert end == next_start


def test_plan_parts_video_shorter_than_part():
    parts = plan_parts(100, 120)
    assert parts == [(0.0, 100.0)]


def test_plan_parts_invalid_inputs():
    with pytest.raises(ValueError):
        plan_parts(0, 120)
    with pytest.raises(ValueError):
        plan_parts(-5, 120)
    with pytest.raises(ValueError):
        plan_parts(600, 0)
    with pytest.raises(ValueError):
        plan_parts("abc", 120)


def test_parse_part_minutes():
    assert parse_part_minutes("2") == 120.0
    assert parse_part_minutes(" 1.5 ") == 90.0
    assert parse_part_minutes("0,5") == 30.0
    with pytest.raises(ValueError):
        parse_part_minutes("")
    with pytest.raises(ValueError):
        parse_part_minutes("abc")
    with pytest.raises(ValueError):
        parse_part_minutes("-2")
    with pytest.raises(ValueError):
        parse_part_minutes("0")


def test_aspect_filter_and_output_size():
    assert "scale=1080:1920" in aspect_filter("9:16")
    assert "pad=1080:1920" in aspect_filter("9:16")
    assert "force_original_aspect_ratio=decrease" in aspect_filter("9:16")
    assert "scale=1920:1080" in aspect_filter("16:9")
    assert output_size("9:16") == (1080, 1920)
    assert output_size("16:9") == (1920, 1080)
    with pytest.raises(ValueError):
        aspect_filter("4:3")
    with pytest.raises(ValueError):
        output_size("4:3")


def test_part_filename_padding():
    assert part_filename(1, 5) == "part_01.mp4"
    assert part_filename(5, 5) == "part_05.mp4"
    assert part_filename(1, 3) == "part_01.mp4"
    assert part_filename(1, 120) == "part_001.mp4"
    assert part_filename(100, 120) == "part_100.mp4"


def test_build_part_command_with_audio(tmp_path):
    cmd = build_part_command(tmp_path / "in.mp4", tmp_path / "out.mp4", 120.0, 60.0, "9:16", has_audio=True)
    assert cmd[0] == "ffmpeg"
    assert cmd.index("-ss") < cmd.index("-i")  # fast seek before input
    assert cmd[cmd.index("-ss") + 1] == "120.000"
    assert cmd[cmd.index("-t") + 1] == "60.000"
    assert "-map" in cmd and "0:v:0" in cmd and "0:a:0?" in cmd
    assert "asetpts=PTS-STARTPTS" in cmd
    assert "aac" in cmd
    assert "libx264" in cmd
    assert cmd[-1].endswith("out.mp4")


def test_build_part_command_without_audio(tmp_path):
    cmd = build_part_command(tmp_path / "in.mp4", tmp_path / "out.mp4", 0.0, 30.0, "16:9", has_audio=False)
    assert "0:a:0?" not in cmd
    assert "aac" not in cmd
    assert "asetpts=PTS-STARTPTS" not in cmd
    assert "1920:1080" in cmd[cmd.index("-vf") + 1]


def test_manifest_roundtrip_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(splitter, "SPLITTER_DIR", tmp_path)
    job_id = f"test-{uuid.uuid4().hex}"
    manifest = {"job_id": job_id, "status": "done", "parts": [{"part": 1, "filename": "part_01.mp4"}]}
    write_manifest(job_id, manifest)
    assert read_manifest(job_id) == manifest
    assert not manifest_path(job_id).with_suffix(".json.tmp").exists()

    manifest_path(job_id).write_text("{not json", encoding="utf-8")
    assert read_manifest(job_id) is None

    assert read_manifest(f"missing-{uuid.uuid4().hex}") is None


def test_delete_job_frees_space(tmp_path, monkeypatch):
    monkeypatch.setattr(splitter, "SPLITTER_DIR", tmp_path)
    job_id = f"test-{uuid.uuid4().hex}"
    folder = job_dir(job_id)
    folder.mkdir(parents=True)
    (folder / "part_01.mp4").write_bytes(b"x" * (1024 * 1024))
    (folder / "manifest.json").write_text("{}", encoding="utf-8")

    freed = delete_job(job_id)
    assert freed >= 1.0
    assert not folder.exists()
    assert delete_job(job_id) == 0.0


def _mock_pipeline(monkeypatch, tmp_path, source_seconds=600.0, fail_on_call=None, write_tmp_before_fail=False):
    from app.services import jobs as jobs_mod

    monkeypatch.setattr(splitter, "SPLITTER_DIR", tmp_path / "splitter")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"v" * 4096)
    monkeypatch.setattr(splitter, "download_video", lambda video_id: video)
    monkeypatch.setattr(splitter, "get_video_duration", lambda p: source_seconds)
    monkeypatch.setattr(splitter, "get_video_dimensions", lambda p: (1920, 1080))
    monkeypatch.setattr(splitter, "_has_audio_stream", lambda p: True)

    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        out = Path(command[-1])
        if fail_on_call is not None and len(calls) == fail_on_call:
            out.write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, command)
        out.write_bytes(b"p" * 2048)

    monkeypatch.setattr(splitter, "run", fake_run)
    return jobs_mod, calls


def test_run_splitter_job_success(tmp_path, monkeypatch):
    jobs_mod, calls = _mock_pipeline(monkeypatch, tmp_path, source_seconds=600.0)
    job_id = jobs_mod.create_job("splitter")
    try:
        manifest = run_splitter_job(job_id, "dQw4w9WgXcQ", 2.0, "9:16", "My Video")
        assert manifest["status"] == "done"
        assert len(manifest["parts"]) == 5
        assert len(calls) == 5
        assert all(p["status"] == "done" and p["size_mb"] is not None for p in manifest["parts"])
        for entry in manifest["parts"]:
            assert (job_dir(job_id) / entry["filename"]).is_file()
        assert not list(job_dir(job_id).glob("*.tmp.mp4"))

        job = jobs_mod.get_job(job_id)
        assert job["status"] == "done" and job["done_clips"] == 5
        assert read_manifest(job_id)["status"] == "done"
    finally:
        jobs_mod.jobs.pop(job_id, None)


def test_run_splitter_job_ffmpeg_failure(tmp_path, monkeypatch):
    jobs_mod, calls = _mock_pipeline(
        monkeypatch, tmp_path, source_seconds=630.0, fail_on_call=1, write_tmp_before_fail=True,
    )
    job_id = jobs_mod.create_job("splitter")
    try:
        manifest = run_splitter_job(job_id, "dQw4w9WgXcQ", 2.0, "16:9", "My Video")
        assert manifest["status"] == "failed"
        assert "part_01.mp4" in manifest["error"]
        assert manifest["parts"][0]["status"] == "failed"
        assert not list(job_dir(job_id).glob("*.tmp.mp4"))
        assert not (job_dir(job_id) / "part_01.mp4").exists()
        assert jobs_mod.get_job(job_id)["status"] == "failed"
    finally:
        jobs_mod.jobs.pop(job_id, None)


def test_run_splitter_job_never_raises_on_download_error(tmp_path, monkeypatch):
    from app.services import jobs as jobs_mod

    monkeypatch.setattr(splitter, "SPLITTER_DIR", tmp_path / "splitter")
    monkeypatch.setattr(splitter, "download_video", lambda video_id: (_ for _ in ()).throw(RuntimeError("download exploded")))
    job_id = jobs_mod.create_job("splitter")
    try:
        manifest = run_splitter_job(job_id, "dQw4w9WgXcQ", 2.0, "9:16", None)
        assert manifest["status"] == "failed"
        assert "download exploded" in manifest["error"]
        assert jobs_mod.get_job(job_id)["status"] == "failed"
    finally:
        jobs_mod.jobs.pop(job_id, None)


def test_get_video_metadata_parses_and_caches(tmp_path, monkeypatch):
    from app.services.video import download as download_mod

    monkeypatch.setattr(download_mod, "TMP_DIR", tmp_path)
    calls = []

    def fake_run(command, capture_output=True, text=True, timeout=60):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="630.5||My Video||1920||1080\n", stderr="")

    monkeypatch.setattr(download_mod, "subprocess", SimpleNamespace(run=fake_run))

    meta = download_mod.get_video_metadata("dQw4w9WgXcQ")
    assert meta == {
        "video_id": "dQw4w9WgXcQ",
        "duration": 630.5,
        "title": "My Video",
        "width": 1920,
        "height": 1080,
    }
    assert len(calls) == 1
    assert "--skip-download" in calls[0]

    cached = download_mod.get_video_metadata("dQw4w9WgXcQ")
    assert cached == meta
    assert len(calls) == 1  # second call served from cache


def test_get_video_metadata_rejects_unavailable_video(tmp_path, monkeypatch):
    from app.services.video import download as download_mod

    monkeypatch.setattr(download_mod, "TMP_DIR", tmp_path)
    monkeypatch.setattr(
        download_mod, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: Private video. Sign in if you've been granted access",
        )),
    )
    with pytest.raises(RuntimeError, match="Could not fetch video info"):
        download_mod.get_video_metadata("dQw4w9WgXcQ")


def test_get_video_metadata_rejects_unknown_duration(tmp_path, monkeypatch):
    from app.services.video import download as download_mod

    monkeypatch.setattr(download_mod, "TMP_DIR", tmp_path)
    monkeypatch.setattr(
        download_mod, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="NA||Live stream||1280||720\n", stderr="",
        )),
    )
    with pytest.raises(RuntimeError, match="duration"):
        download_mod.get_video_metadata("dQw4w9WgXcQ")


def test_ensure_disk_space_fails_when_tight(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"v" * 1024)
    monkeypatch.setattr(
        splitter.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=1024 * 1024),
    )
    monkeypatch.setattr(splitter.settings, "SPLITTER_MIN_DISK_FREE_MB", 500)
    with pytest.raises(RuntimeError, match="Not enough disk space"):
        splitter._ensure_disk_space(video)
