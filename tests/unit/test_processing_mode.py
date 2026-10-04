"""Tests for 9:16 processing modes: face_detection vs no_face_detection."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


def test_no_face_mode_uses_pad_not_crop(tmp_path, monkeypatch):
    import app.services.video.render as render_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "clip.mp4"
    seen = {}

    monkeypatch.setattr(render_mod, "get_video_dimensions", lambda _: (1920, 1080))

    def fake_run(cmd):
        seen["cmd"] = list(cmd)
        out.write_bytes(b"video")

    monkeypatch.setattr(render_mod, "run", fake_run)

    # Ensure face detection would fail loudly if called.
    import app.services.video.faces as faces_mod
    monkeypatch.setattr(
        faces_mod, "detect_face_center_x", lambda *a, **k: (_ for _ in ()).throw(AssertionError("face detection must not run"))
    )

    render_mod.render_video(src, out, 0.0, 5.0, vertical_crop=True, processing_mode="no_face_detection")

    cmd = seen["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    assert "pad=1080:1920" in vf
    assert "color=black" in vf
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in vf
    assert "crop=" not in vf


def test_face_mode_uses_crop_not_pad(tmp_path, monkeypatch):
    import app.services.video.render as render_mod
    import app.services.video.faces as faces_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "clip.mp4"
    seen = {}

    monkeypatch.setattr(render_mod, "get_video_dimensions", lambda _: (1920, 1080))
    monkeypatch.setattr(faces_mod, "detect_face_center_x", lambda *a, **k: 960.0)

    def fake_run(cmd):
        seen["cmd"] = list(cmd)
        out.write_bytes(b"video")

    monkeypatch.setattr(render_mod, "run", fake_run)

    render_mod.render_video(src, out, 0.0, 5.0, vertical_crop=True, processing_mode="face_detection")

    cmd = seen["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=" in vf
    assert "scale=1080:1920" in vf
    assert "pad=1080:1920" not in vf


def test_no_vertical_crop_keeps_original(tmp_path, monkeypatch):
    import app.services.video.render as render_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "clip.mp4"
    seen = {}

    monkeypatch.setattr(render_mod, "get_video_dimensions", lambda _: (1920, 1080))

    def fake_run(cmd):
        seen["cmd"] = list(cmd)
        out.write_bytes(b"video")

    monkeypatch.setattr(render_mod, "run", fake_run)

    render_mod.render_video(src, out, 0.0, 5.0, vertical_crop=False, processing_mode="no_face_detection")

    cmd = seen["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=" not in vf
    assert "pad=1080:1920" not in vf


def test_invalid_processing_mode_rejected(tmp_path):
    import pytest

    import app.services.video.render as render_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "clip.mp4"

    with pytest.raises(ValueError):
        render_mod.render_video(src, out, 0.0, 5.0, vertical_crop=True, processing_mode="bogus")


def test_schemas_default_face_detection():
    from app.api.schemas import ClipRequest, SubtitleRequest, ViralClipsRequest

    assert ClipRequest(url="https://youtu.be/x", start="0", end="5").processing_mode == "face_detection"
    assert ViralClipsRequest(url="https://youtu.be/x").processing_mode == "face_detection"
    assert SubtitleRequest(url="https://youtu.be/x").processing_mode == "face_detection"


def test_telegram_proc_routing_ai_and_timestamp(monkeypatch):
    import asyncio

    from app.telegram import handlers_buttons as buttons_mod

    async def fake_auth(_update):
        return False

    monkeypatch.setattr(buttons_mod, "reject_if_unauthorized", fake_auth)

    for mode, runner_name in (("ai", "run_ai_generation"), ("timestamp", "run_timestamp_generation")):
        calls = {}

        async def fake_runner(query, context):
            calls["ran"] = True

        monkeypatch.setattr(buttons_mod, runner_name, fake_runner)
        try:
            class FakeQuery:
                data = "proc:no_face_detection"

                async def answer(self):
                    pass

                async def edit_message_text(self, *a, **k):
                    pass

            class FakeMessage:
                chat_id = 123
                message_id = 1

            FakeQuery.message = FakeMessage()

            class FakeUpdate:
                callback_query = FakeQuery()
                effective_chat = None

            class FakeContext:
                user_data = {"mode": mode, "subtitles": "native"}

            asyncio.run(buttons_mod.telegram_button(FakeUpdate(), FakeContext()))
            assert FakeContext.user_data["processing_mode"] == "no_face_detection"
            assert calls.get("ran") is True
        finally:
            pass
