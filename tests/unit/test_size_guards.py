"""Regression tests for the Telegram 50 MB failure (production logs 2026-09-17).

Gemini returned 125-171s clips despite its 20-60s prompt rule, the render
used unbounded H.264 defaults (78-108 MB files), and sendVideo rejected
every one with "Request Entity Too Large".
"""

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


def test_target_maxrate_fits_telegram_budget():
    from app.config import settings
    from app.services.video.render import target_video_maxrate_kbps

    # Durations straight from the failing production batch (137s/125s/145s/171s)
    # plus a normal 60s short.
    for duration in (60.0, 125.0, 137.0, 145.0, 171.0):
        rate = target_video_maxrate_kbps(duration)
        assert 500 <= rate <= 2500
        # muxed estimate (video + 128k audio) must stay under the cap
        est_mb = (rate + 128) * duration / 8 / 1024
        assert est_mb <= settings.TELEGRAM_MAX_VIDEO_MB, duration


def test_render_passes_bounded_encode_flags(tmp_path, monkeypatch):
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
    monkeypatch.setattr(render_mod, "is_over_telegram_limit", lambda _p: False)

    render_mod.render_video(src, out, 0.0, 137.0, vertical_crop=False)

    cmd = seen["cmd"]
    assert "-crf" in cmd
    assert cmd[cmd.index("-crf") + 1] == str(render_mod.settings.RENDER_CRF)
    assert "-maxrate" in cmd and cmd[cmd.index("-maxrate") + 1].endswith("k")
    assert "-bufsize" in cmd
    assert "-pix_fmt" in cmd and "yuv420p" in cmd
    assert "+faststart" in " ".join(cmd)
    assert "-b:a" in cmd


def test_render_recompresses_oversize_output(tmp_path, monkeypatch):
    import app.services.video.render as render_mod

    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    out = tmp_path / "clip.mp4"
    calls = []

    monkeypatch.setattr(render_mod, "get_video_dimensions", lambda _: (1920, 1080))
    monkeypatch.setattr(
        render_mod, "run", lambda cmd: out.write_bytes(b"video")
    )
    monkeypatch.setattr(render_mod, "is_over_telegram_limit", lambda _p: True)
    monkeypatch.setattr(
        render_mod, "compress_for_telegram", lambda p: calls.append(p) or True
    )

    render_mod.render_video(src, out, 0.0, 60.0, vertical_crop=False)
    assert calls == [out]


def test_viral_clamps_overlong_candidate(monkeypatch, tmp_path):
    import app.services.viral as viral_mod
    from app.config import settings

    # The exact failing candidate shape from production: 01:07:32 -> 01:09:49 (137s)
    candidate = {
        "title": "t", "start": "01:07:32", "end": "01:09:49",
        "score": 98, "reason": "r", "hashtags": [], "description": "d",
    }
    rendered = {}

    monkeypatch.setattr(viral_mod, "prepare", lambda url: ("vid", tmp_path / "src.mp4"))
    monkeypatch.setattr(viral_mod, "analyze_viral_clips", lambda *a: [candidate])
    monkeypatch.setattr(viral_mod, "resolve_subtitle_words", lambda *a: None)

    def fake_render(video_path, output_path, start, end, words=None, vertical_crop=True):
        rendered["start"] = start
        rendered["end"] = end

    monkeypatch.setattr(viral_mod, "render_video", fake_render)

    async def fake_send(**kwargs):
        return True

    import app.telegram.sending as sending_mod
    monkeypatch.setattr(sending_mod, "send_clip_to_telegram", fake_send)

    req = SimpleNamespace(url="https://youtu.be/x", max_clips=10,
                          vertical_crop=True, subtitles="none")
    video_id, entries = asyncio.run(viral_mod.generate_viral_clips(req))

    assert video_id == "vid"
    assert len(entries) == 1
    assert rendered["end"] - rendered["start"] == settings.MAX_CLIP_DURATION_SEC


class _FakeBot:
    def __init__(self, send_behavior=None):
        self.send_behavior = send_behavior
        self.video_calls = 0
        self.messages = []

    async def send_video(self, **kwargs):
        self.video_calls += 1
        if self.send_behavior == "too_large":
            from telegram.error import NetworkError
            raise NetworkError("Request Entity Too Large")
        return True

    async def send_message(self, **kwargs):
        self.messages.append(kwargs.get("text", ""))
        return True


def _patch_bot(monkeypatch, fake):
    import app.telegram.bot as bot_mod

    monkeypatch.setattr(bot_mod, "telegram_bot", fake)
    monkeypatch.setattr(bot_mod, "get_bot", lambda: fake)


def _clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 100)
    return p


def test_sending_skips_oversize_file_without_send_attempt(monkeypatch, tmp_path):
    import app.telegram.sending as sending
    from app.config import settings as app_settings

    fake = _FakeBot()
    _patch_bot(monkeypatch, fake)
    # Shrink the cap so a 100-byte file counts as oversized without
    # writing 49 MB to disk.
    monkeypatch.setattr(app_settings, "TELEGRAM_MAX_VIDEO_MB", 0.00001)

    import app.services.video.render as render_mod
    monkeypatch.setattr(render_mod, "compress_for_telegram", lambda _p: False)

    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path), "vid-1", "Title", 90, "reason",
    ))
    assert sent is False
    assert fake.video_calls == 0
    assert any("50 MB" in m for m in fake.messages)


def test_sending_reports_api_too_large_honestly(monkeypatch, tmp_path):
    import app.telegram.sending as sending

    fake = _FakeBot(send_behavior="too_large")
    _patch_bot(monkeypatch, fake)

    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path), "vid-2", "Title", 90, "reason",
    ))
    assert sent is False
    assert fake.video_calls == 1  # no pointless retries on a size rejection
    assert any("50 MB" in m for m in fake.messages)
