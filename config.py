"""Central validated settings (pydantic-settings).

Reads from environment / .env. Existing os.getenv reads in video_utils.py
remain for backward compat, but new code should import from here.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""

    GEMINI_API_KEY: str = ""

    WHISPER_MODEL_SIZE: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"

    FFMPEG_PRESET: str = "veryfast"
    LOG_LEVEL: str = "INFO"
    YOUTUBE_COOKIES_FILE: str = ""

    # YouTube Data API v3 uploads (Unlisted) for approved Telegram clips.
    YT_CLIENT_ID: str = ""
    YT_CLIENT_SECRET: str = ""
    YT_REFRESH_TOKEN: str = ""
    YT_CATEGORY_ID: str = "22"

    def allowed_chat_ids(self) -> set[str]:
        ids = {c.strip() for c in self.TELEGRAM_ALLOWED_CHAT_IDS.split(",") if c.strip()}
        if self.TELEGRAM_CHAT_ID:
            ids.add(self.TELEGRAM_CHAT_ID.strip())
        return ids


settings = Settings()
