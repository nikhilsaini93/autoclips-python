import asyncio
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


class FakeBot:
    def __init__(self):
        self.videos = 0
        self.last_kwargs = None

    async def send_video(self, **kwargs):
        self.videos += 1
        self.last_kwargs = kwargs
        return True

    async def send_message(self, **kwargs):
        return True


def _clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 100)
    return p


def test_send_registers_all_three_tokens_when_configured(monkeypatch, tmp_path):
    import app.telegram.bot as bot_mod
    import app.telegram.sending as sending
    from app.services import approvals
    from telegram import InlineKeyboardMarkup

    approvals.pending_uploads.clear()
    fake = FakeBot()
    monkeypatch.setattr(bot_mod, "telegram_bot", fake)
    monkeypatch.setattr(bot_mod, "get_bot", lambda: fake)
    monkeypatch.setattr("app.services.meta.tokens.is_instagram_configured", lambda: True)
    monkeypatch.setattr("app.services.meta.tokens.is_facebook_configured", lambda: True)
    # sending.py imports the helpers inside the function; patch where looked up.
    import app.services.meta.tokens as tokens

    monkeypatch.setattr(tokens, "is_instagram_configured", lambda: True)
    monkeypatch.setattr(tokens, "is_facebook_configured", lambda: True)

    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path), "vid-1", "Title", 90, "reason", ["a"], "desc",
    ))
    assert sent is True
    assert len(approvals.pending_uploads) == 3
    kb = fake.last_kwargs["reply_markup"]
    assert isinstance(kb, InlineKeyboardMarkup)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c.startswith("approve:") for c in callbacks)
    assert any(c.startswith("approve_ig:") for c in callbacks)
    assert any(c.startswith("approve_fb:") for c in callbacks)
    assert any(c.startswith("deny:") for c in callbacks)
    approvals.pending_uploads.clear()


def test_send_yt_only_when_meta_unconfigured(monkeypatch, tmp_path):
    import app.telegram.bot as bot_mod
    import app.telegram.sending as sending
    from app.services import approvals

    approvals.pending_uploads.clear()
    fake = FakeBot()
    monkeypatch.setattr(bot_mod, "telegram_bot", fake)
    monkeypatch.setattr(bot_mod, "get_bot", lambda: fake)
    import app.services.meta.tokens as tokens

    monkeypatch.setattr(tokens, "is_instagram_configured", lambda: False)
    monkeypatch.setattr(tokens, "is_facebook_configured", lambda: False)

    sent = asyncio.run(sending.send_clip_to_telegram(
        _clip(tmp_path), "vid-2", "Title", 90, "reason",
    ))
    assert sent is True
    assert len(approvals.pending_uploads) == 1
    callbacks = [b.callback_data for row in fake.last_kwargs["reply_markup"].inline_keyboard for b in row]
    assert not any(c.startswith("approve_ig:") for c in callbacks)
    approvals.pending_uploads.clear()
