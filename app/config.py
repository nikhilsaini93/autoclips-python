"""Central validated settings + repo paths (pydantic-settings).

Reads from environment / .env. All code should import from here instead of
using os.getenv directly.
"""

from pathlib import Path

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

    FACE_CONF_THRESHOLD: float = 0.5
    FACE_DETECT_SAMPLES: int = 3
    SKIP_FACE_DETECT: bool = False

    def allowed_chat_ids(self) -> set[str]:
        ids = {c.strip() for c in self.TELEGRAM_ALLOWED_CHAT_IDS.split(",") if c.strip()}
        if self.TELEGRAM_CHAT_ID:
            ids.add(self.TELEGRAM_CHAT_ID.strip())
        return ids


settings = Settings()

# ---------------------------------------------------------------------------
# Repo paths — single source of truth for storage + assets locations.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent

STORAGE_ROOT = ROOT / "storage"
DOWNLOAD_DIR = STORAGE_ROOT / "downloads"
CLIPS_DIR = STORAGE_ROOT / "clips"
TMP_DIR = STORAGE_ROOT / "tmp"
VIDEOS_DIR = STORAGE_ROOT / "videos"
LOGS_DIR = STORAGE_ROOT / "logs"

ASSETS_FACE_DIR = ROOT / "assets" / "face_detector"
PROTOTXT_PATH = ASSETS_FACE_DIR / "deploy.prototxt"
CAFFEMODEL_PATH = ASSETS_FACE_DIR / "res10_300x300_ssd_iter_140000.caffemodel"

for _d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, VIDEOS_DIR, LOGS_DIR, ASSETS_FACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
