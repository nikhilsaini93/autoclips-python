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
    # T4 / CUDA tuning: VAD removes silence/hallucinations and gives cleaner
    # word boundaries for the silence snapper. Disable only for debugging.
    WHISPER_VAD_FILTER: bool = True
    WHISPER_MIN_SILENCE_MS: int = 400
    WHISPER_SPEECH_PAD_MS: int = 200

    FFMPEG_PRESET: str = "veryfast"
    # Render timeout: 0 = auto (max(600s, 30x clip duration) — slow CPU
    # software-x264 needs ~10x realtime for 1080x1920 + libass + loudnorm,
    # and viral batches run 2 encodes in parallel). Set an explicit seconds
    # value to override (production TimeoutExpired 2026-09-18: 56s clip
    # killed by the old flat 600s cap on loaded Colab CPU).
    RENDER_TIMEOUT_SEC: float = 0.0
    # Bounded H.264 encode: keeps 60-170s clips under the Telegram Bot API
    # ~50MB sendVideo cap without blocky defaults (x264 crf23 veryfast).
    RENDER_CRF: int = 20
    RENDER_AUDIO_BITRATE: str = "128k"
    RENDER_FPS: int = 30
    # Telegram Bot API sendVideo cap (paid/local Bot API allows up to 2GB —
    # raise this via env instead of code change).
    TELEGRAM_MAX_VIDEO_MB: float = 50.0
    # Viral clip length guard: Gemini is prompted 20-60s but occasionally
    # returns 120s+ picks that fail Telegram 50MB + FB 90s — clamp in viral flow.
    MAX_CLIP_DURATION_SEC: float = 60.0
    MIN_CLIP_DURATION_SEC: float = 15.0
    # Optional token guarding POST /admin/cleanup (empty = open, for backwards
    # compat — set it to require ?token=...).
    ADMIN_TOKEN: str = ""
    LOG_LEVEL: str = "INFO"
    YOUTUBE_COOKIES_FILE: str = ""

    # YouTube Data API v3 uploads (Unlisted) for approved Telegram clips.
    YT_CLIENT_ID: str = ""
    YT_CLIENT_SECRET: str = ""
    YT_REFRESH_TOKEN: str = ""
    YT_CATEGORY_ID: str = "22"

    # Meta uploads (Instagram Reels + Facebook Page Reels) for approved clips.
    # Setup: Meta App + FB Page linked to IG Professional → long-lived Page
    # token via scripts/get_meta_token.py (see docs/META_SETUP.md).
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_PAGE_TOKEN: str = ""
    FB_PAGE_ID: str = ""
    IG_USER_ID: str = ""
    META_API_VERSION: str = "v26.0"
    # Share IG reels to profile feed as well as Reels tab.
    META_IG_SHARE_TO_FEED: bool = True
    # FB Reels must be 3-90s. If True, longer clips fall back to a regular
    # Page video post instead of failing.
    META_FB_FALLBACK_TO_VIDEO: bool = True

    FACE_CONF_THRESHOLD: float = 0.5
    FACE_DETECT_SAMPLES: int = 3
    SKIP_FACE_DETECT: bool = False
    # T4 face pipeline: yolo (ultralytics YOLOv8n-face on CUDA) > yunet
    # (cv2.FaceDetectorYN, no new dep) > res10 (legacy Caffe fallback).
    FACE_MODEL: str = "yolo"
    # Dense sampling rate for vertical-crop tracks. 1fps is the T4 sweet
    # spot; CPU boxes should set 0.3 or keep FACE_DETECT_SAMPLES fallback.
    FACE_SAMPLE_FPS: float = 1.0
    # Moving-average window (in samples) for smoothing the crop center x(t).
    FACE_SMOOTH_WINDOW: int = 5
    # Max horizontal pan speed (px/sec at source resolution) to avoid jitter.
    FACE_MAX_PAN_PX_PER_SEC: float = 200.0
    SMOOTH_CROP: bool = True
    # Snap AI cut boundaries to sentence ends / silences instead of mid-word.
    SNAP_TO_SILENCE: bool = True
    SNAP_WINDOW_SEC: float = 0.8

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
YUNET_PATH = ASSETS_FACE_DIR / "face_detection_yunet_2023mar.onnx"

for _d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, VIDEOS_DIR, LOGS_DIR, ASSETS_FACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
