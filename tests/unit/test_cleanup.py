import os
import time
from pathlib import Path


def test_cleanup_old_files(tmp_path: Path, monkeypatch):
    import app.services.video.cleanup as cleanup_mod

    dl = tmp_path / "downloads"
    cl = tmp_path / "clips"
    tm = tmp_path / "tmp"
    vd = tmp_path / "videos"
    dl.mkdir()
    cl.mkdir()
    tm.mkdir()
    vd.mkdir()
    monkeypatch.setattr(cleanup_mod, "DOWNLOAD_DIR", dl)
    monkeypatch.setattr(cleanup_mod, "CLIPS_DIR", cl)
    monkeypatch.setattr(cleanup_mod, "TMP_DIR", tm)
    monkeypatch.setattr(cleanup_mod, "VIDEOS_DIR", vd)

    old = cl / "old.mp4"
    old.write_bytes(b"x" * 100)

    ancient = time.time() - 100 * 3600
    os.utime(old, (ancient, ancient))

    result = cleanup_mod.cleanup_old_files(max_age_hours=72)
    assert result["deleted"] >= 1
    assert not old.exists()
