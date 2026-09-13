import asyncio
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


class FakeBot:
    def __init__(self, behavior):
        self.behavior = list(behavior)
        self.calls = 0
        self.last_kwargs = None

    async def send_video(self, **kwargs):
        from telegram.error import TimedOut

        self.calls += 1
        self.last_kwargs = kwargs
        action = self.behavior.pop(0) if self.behavior else "ok"
        if action == "timeout":
            raise TimedOut("timed out")
        return True

    async def send_message(self, **kwargs):
        return True


def _clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 100)
    return p


def test_send_retries_timeout_then_succeeds(monkeypatch, tmp_path):
    import main as m
    from telegram import InlineKeyboardMarkup

    monkeypatch.setattr(m, "telegram_bot", FakeBot(["timeout", "timeout", "ok"]))
    sent = asyncio.run(m.send_clip_to_telegram(
        _clip(tmp_path),
        "vid-1", "Title", 90, "reason", ["a"], "desc",
    ))
    assert sent is True
    assert m.telegram_bot.calls == 3
    assert isinstance(m.telegram_bot.last_kwargs["reply_markup"], InlineKeyboardMarkup)


def test_send_gives_up_and_drops_token(monkeypatch, tmp_path):
    import main as m

    monkeypatch.setattr(m, "telegram_bot", FakeBot(["timeout"] * 10))
    before = set(m.pending_uploads)
    sent = asyncio.run(m.send_clip_to_telegram(
        _clip(tmp_path), "vid-9", "Title", 90, "reason",
    ))
    assert sent is False
    assert m.telegram_bot.calls == 3
    # No leaked approval token for the undelivered clip.
    assert set(m.pending_uploads) == before
