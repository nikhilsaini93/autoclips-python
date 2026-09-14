import logging
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

load_dotenv()

from app.api.deps import limiter
from app.api.routes_analyze import router as analyze_router
from app.api.routes_clip import router as clip_router
from app.api.routes_health import router as health_router
from app.api.routes_subtitles import router as subtitles_router
from app.api.routes_viral import router as viral_router
from app.config import settings
from app.logging_config import request_id_var, setup_logging

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.telegram.bot import _polling_error_callback, get_app
    from app.telegram.handlers_buttons import telegram_button
    from app.telegram.handlers_start import telegram_start, telegram_text_router

    telegram_app = get_app()
    telegram_app.add_handler(CommandHandler("start", telegram_start))
    telegram_app.add_handler(CallbackQueryHandler(telegram_button))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text_router)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    # timeout=30 (long-poll hold) must stay below get_updates read_timeout
    # (65s). poll_interval avoids hammering on fast failures, bootstrap
    # retries cover startup blips, and the custom error_callback downgrades
    # transient NetworkError/TimedOut to a warning (auto-retried anyway).
    await telegram_app.updater.start_polling(
        poll_interval=2.0,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
        error_callback=_polling_error_callback,
    )

    logger.info("Telegram bot polling started")

    if not (settings.YT_CLIENT_ID and settings.YT_CLIENT_SECRET and settings.YT_REFRESH_TOKEN):
        logger.warning(
            "YouTube upload NOT configured (YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN missing) — "
            "Telegram Approve will fail until you run scripts/get_youtube_token.py (see docs/YOUTUBE_SETUP.md)."
        )

    yield

    logger.info("Stopping Telegram bot")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)

# Rate limiting (no auth per scope — expensive routes get per-IP caps).
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda request, exc: JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"}),
)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Tags every log line produced while handling this request with a short
    request id (visible in console + storage/logs/app.log), and logs start/end/timing."""
    req_id = uuid.uuid4().hex[:8]
    token = request_id_var.set(req_id)
    t0 = time.perf_counter()
    logger.info("--> %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("!! %s %s failed after %.1fs", request.method, request.url.path, time.perf_counter() - t0)
        request_id_var.reset(token)
        raise
    elapsed = time.perf_counter() - t0
    logger.info("<-- %s %s %d in %.1fs", request.method, request.url.path, response.status_code, elapsed)
    response.headers["X-Request-ID"] = req_id
    request_id_var.reset(token)
    return response


app.include_router(health_router)
app.include_router(subtitles_router)
app.include_router(clip_router)
app.include_router(analyze_router)
app.include_router(viral_router)
