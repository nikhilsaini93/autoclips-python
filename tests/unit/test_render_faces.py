from pathlib import Path


def test_render_video_creates_parent_dir(tmp_path: Path, monkeypatch):
    import app.services.video.render as render_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "missing" / "clip.mp4"

    monkeypatch.setattr(render_mod, "get_video_dimensions", lambda _: (1920, 1080))

    def fake_run(cmd):
        assert out.parent.exists()
        out.write_bytes(b"video")

    monkeypatch.setattr(render_mod, "run", fake_run)

    render_mod.render_video(src, out, 1.0, 3.0, vertical_crop=False)
    assert out.parent.exists()
    assert out.exists()


def test_detect_face_center_x_does_not_use_unsupported_vsync(tmp_path: Path, monkeypatch):
    import app.services.video.faces as faces
    from app.services.video.faces import detect_face_center_x

    seen = []

    def fake_run(cmd):
        seen.append(cmd)

    monkeypatch.setattr(faces, "run", fake_run)
    monkeypatch.setattr(faces, "detect_face_center_x_in_frame", lambda _frame_path, _frame_width: 100.0)

    detect_face_center_x(tmp_path / "video.mp4", 0.0, 10.0, 200)

    assert seen
    assert all("-vsync" not in cmd for cmd in seen)
