import json
import logging
import os
import threading
import time
from pathlib import Path

from app.config import TMP_DIR, settings

logger = logging.getLogger(__name__)

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                logger.info(
                    "Loading Whisper model '%s' (device=%s, compute=%s, first call only)...",
                    settings.WHISPER_MODEL_SIZE,
                    settings.WHISPER_DEVICE,
                    settings.WHISPER_COMPUTE_TYPE,
                )
                t0 = time.perf_counter()
                _whisper_model = WhisperModel(
                    settings.WHISPER_MODEL_SIZE,
                    device=settings.WHISPER_DEVICE,
                    compute_type=settings.WHISPER_COMPUTE_TYPE,
                )
                logger.info("Whisper model loaded in %.1fs", time.perf_counter() - t0)
    return _whisper_model


def _transcribe_words(video_id: str, video_path: Path, language, task: str) -> dict:
    """Runs Whisper and returns {"language": <detected>, "words": [...]}.
    Cached to disk per (video_id, language, task) since transcription is the
    slowest step and every route/clip on the same source video reuses it."""
    cache_key = f"{video_id}-{language or 'auto'}-{task}"
    cache_path = TMP_DIR / f"{cache_key}-words.json"
    if cache_path.exists():
        try:
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(result.get("words"), list):
                logger.info("Using cached transcript for %s (%d words, language=%s)",
                            cache_key, len(result["words"]), result.get("language"))
                return result
            logger.warning("Transcript cache %s has bad shape, re-transcribing", cache_path)
        except (OSError, ValueError) as e:
            logger.warning("Ignoring corrupt transcript cache %s (%s), re-transcribing", cache_path, e)
        try:
            cache_path.unlink()
        except OSError:
            pass

    logger.info("Transcribing video_id=%s (language=%s, task=%s) with Whisper...", video_id, language or "auto", task)
    t0 = time.perf_counter()
    model = get_whisper_model()
    segments, info = model.transcribe(
        str(video_path), word_timestamps=True, language=language, task=task
    )

    words = []
    segment_count = 0
    for segment in segments:
        segment_count += 1
        for word in segment.words:
            words.append({"start": word.start, "end": word.end, "text": word.word.strip()})

    detected_language = getattr(info, "language", language or "unknown")
    result = {"language": detected_language, "words": words}
    tmp_cache = cache_path.with_suffix(".json.tmp")
    tmp_cache.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp_cache, cache_path)
    logger.info(
        "Transcription done for %s: %d segments, %d words, detected_language=%s, in %.1fs",
        cache_key, segment_count, len(words), detected_language, time.perf_counter() - t0,
    )
    return result


def transcribe_words_native(video_id: str, video_path: Path) -> dict:
    """Auto-detects the spoken language and transcribes in that language's
    native script (no translation). Used to feed the viral-clip finder."""
    return _transcribe_words(video_id, video_path, language=None, task="transcribe")


def transcribe_words_english(video_id: str, video_path: Path) -> list:
    """Whisper's 'translate' task converts speech in any source language straight to English text."""
    return _transcribe_words(video_id, video_path, language=None, task="translate")["words"]


def words_to_transcript_text(words: list) -> str:
    """Groups words into ~12-word lines with a leading timestamp, the
    '[seconds] text...' shape the viral-clip-finding prompt expects."""
    lines = []
    buffer = []
    line_start = None

    for w in words:
        if line_start is None:
            line_start = w["start"]
        buffer.append(w["text"])
        if len(buffer) >= 12:
            lines.append(f"[{int(line_start)}] {' '.join(buffer)}")
            buffer = []
            line_start = None

    if buffer:
        lines.append(f"[{int(line_start)}] {' '.join(buffer)}")

    return "\n".join(lines)
