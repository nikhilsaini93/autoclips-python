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
    import app.telegram.bot as bot_mod
    import app.telegram.sending as sending
    from telegram import InlineKeyboardMarkup

    fake = FakeBot(["timeout", "timeout", "ok"])
    monkeypatch.setattr(bot_mod, "telegram_bot", fake)
    monkeypatch.setattr(bot_mod, "get_bot", lambda: fake)
    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path),
        "vid-1", "Title", 90, "reason", ["a"], "desc",
    ))
    assert sent is True
    assert fake.calls == 3
    assert isinstance(fake.last_kwargs["reply_markup"], InlineKeyboardMarkup)


def test_send_gives_up_and_drops_token(monkeypatch, tmp_path):
    import app.telegram.bot as bot_mod
    import app.telegram.sending as sending
    from app.services.approvals import pending_uploads

    fake = FakeBot(["timeout"] * 10)
    monkeypatch.setattr(bot_mod, "telegram_bot", fake)
    monkeypatch.setattr(bot_mod, "get_bot", lambda: fake)
    before = set(pending_uploads)
    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path), "vid-9", "Title", 90, "reason",
    ))
    assert sent is False
    assert fake.calls == 3
    # No leaked approval token for the undelivered clip.
    assert set(pending_uploads) == before
