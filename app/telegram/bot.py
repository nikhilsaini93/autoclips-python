import logging

from telegram import Bot
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import Application

from app.config import settings

logger = logging.getLogger(__name__)

telegram_bot: Bot | None = None
telegram_app: Application | None = None


def _polling_error_callback(exc: TelegramError) -> None:
    """Error callback for Updater polling — must be sync (not async).

    Transient network blips (httpx.ReadError wrapped as NetworkError,
    TimedOut) are expected during long jobs / flaky links and are
    retried automatically by PTB's network_retry_loop. Log them as a
    one-line warning instead of the default full ERROR traceback so
    logs stay readable. Anything else keeps the full traceback.
    """
    if isinstance(exc, (NetworkError, TimedOut)):
        logging.getLogger("telegram.ext.Updater").warning(
            "Polling hiccup (%s: %s) — retrying automatically, jobs keep running.",
            exc.__class__.__name__,
            exc,
        )
        return
    logging.getLogger("telegram.ext.Updater").exception(
        "Exception happened while polling for updates.", exc_info=exc
    )


def build_telegram() -> Application:
    """Builds (once) the global Bot + Application. Raises if token missing."""
    global telegram_bot, telegram_app
    if telegram_app is not None:
        return telegram_app
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file before starting the app."
        )
    from telegram.request import HTTPXRequest

    tg_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=180.0,
        pool_timeout=30.0,
    )
    tg_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=30.0,
        read_timeout=65.0,
        write_timeout=60.0,
        pool_timeout=30.0,
    )
    telegram_bot = Bot(token=token, request=tg_request)

    telegram_app = (
        Application.builder()
        .token(token)
        .request(tg_request)  # long write timeouts for send_video
        .get_updates_request(tg_updates_request)  # read_timeout (65s) > polling timeout (30s)
        .build()
    )
    return telegram_app


def get_bot() -> Bot | None:
    if telegram_bot is None and settings.TELEGRAM_BOT_TOKEN:
        build_telegram()
    return telegram_bot


def get_app() -> Application:
    if telegram_app is None:
        return build_telegram()
    return telegram_app
