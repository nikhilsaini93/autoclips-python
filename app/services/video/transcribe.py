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
    """Runs Whisper and returns {"language", "words", "segments", "silences"}.

    Cached to disk per (video_id, language, task) since transcription is the
    slowest step and every route/clip on the same source video reuses it.
    v2 cache adds sentence segments + silence gaps for the silence snapper
    and the structured Gemini prompt; v1 caches (words-only) are upgraded
    on the fly so old Drive caches keep working."""
    cache_key = f"{video_id}-{language or 'auto'}-{task}"
    cache_path = TMP_DIR / f"{cache_key}-words.json"
    if cache_path.exists():
        try:
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(result.get("words"), list):
                upgraded = _ensure_v2_shape(result)
                logger.info("Using cached transcript for %s (%d words, %d segments, %d silences, language=%s)",
                            cache_key, len(upgraded["words"]), len(upgraded.get("segments", [])),
                            len(upgraded.get("silences", [])), upgraded.get("language"))
                return upgraded
            logger.warning("Transcript cache %s has bad shape, re-transcribing", cache_path)
        except (OSError, ValueError) as e:
            logger.warning("Ignoring corrupt transcript cache %s (%s), re-transcribing", cache_path, e)
        try:
            cache_path.unlink()
        except OSError:
            pass

    logger.info("Transcribing video_id=%s (language=%s, task=%s, vad=%s) with Whisper...",
                video_id, language or "auto", task, settings.WHISPER_VAD_FILTER)
    t0 = time.perf_counter()
    model = get_whisper_model()
    transcribe_kwargs: dict = dict(
        word_timestamps=True, language=language, task=task,
    )
    if settings.WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_filter"] = True
        transcribe_kwargs["vad_parameters"] = dict(
            min_silence_duration_ms=settings.WHISPER_MIN_SILENCE_MS,
            speech_pad_ms=settings.WHISPER_SPEECH_PAD_MS,
        )
    segments, info = model.transcribe(str(video_path), **transcribe_kwargs)

    words = []
    segments_out = []
    segment_count = 0
    for segment in segments:
        segment_count += 1
        seg_words = []
        for word in segment.words or []:
            text = (word.word or "").strip()
            if not text:
                continue
            w = {"start": float(word.start), "end": float(word.end), "text": text}
            words.append(w)
            seg_words.append(w)
        seg_text = (segment.text or "").strip()
        if not seg_text and seg_words:
            seg_text = " ".join(w["text"] for w in seg_words)
        if seg_words or seg_text:
            segments_out.append({
                "start": float(seg_words[0]["start"]) if seg_words else float(segment.start or 0.0),
                "end": float(seg_words[-1]["end"]) if seg_words else float(segment.end or 0.0),
                "text": seg_text,
            })

    words.sort(key=lambda w: w["start"])
    detected_language = getattr(info, "language", language or "unknown")
    silences = compute_silences(words, min_gap_sec=max(0.3, settings.WHISPER_MIN_SILENCE_MS / 1000.0))
    result = {
        "language": detected_language,
        "words": words,
        "segments": segments_out,
        "silences": silences,
        "model": settings.WHISPER_MODEL_SIZE,
        "cache_version": 2,
    }
    tmp_cache = cache_path.with_suffix(".json.tmp")
    tmp_cache.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp_cache, cache_path)
    logger.info(
        "Transcription done for %s: %d segments, %d words, %d silences, detected_language=%s, in %.1fs",
        cache_key, segment_count, len(words), len(silences), detected_language, time.perf_counter() - t0,
    )
    return result


def _ensure_v2_shape(result: dict) -> dict:
    """Upgrades v1 (words-only) caches to v2 in memory.

    Old Drive caches keep working without a costly re-transcribe; the v2
    fields are derived deterministically from word timestamps."""
    if isinstance(result.get("segments"), list) and isinstance(result.get("silences"), list):
        return result
    words = result.get("words") or []
    result["segments"] = words_to_segments(words)
    result["silences"] = compute_silences(words)
    result.setdefault("cache_version", 2)
    return result


def compute_silences(words: list, min_gap_sec: float = 0.4) -> list:
    """Returns [{"start","end","duration"}] gaps between consecutive words."""
    silences = []
    for prev, cur in zip(words, words[1:]):
        try:
            gap_start = float(prev["end"])
            gap_end = float(cur["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if gap_end - gap_start >= min_gap_sec:
            silences.append({"start": gap_start, "end": gap_end, "duration": round(gap_end - gap_start, 2)})
    return silences


def words_to_segments(words: list, max_words: int = 24) -> list:
    """Fallback sentence-ish segments derived from word gaps (for v1 caches).

    Splits on end-of-sentence punctuation or gaps >= 0.6s so the Gemini
    prompt still gets boundary cues without re-transcribing."""
    segments = []
    buf = []
    for i, w in enumerate(words):
        buf.append(w)
        text = w.get("text", "")
        if i + 1 < len(words):
            try:
                gap = float(words[i + 1]["start"]) - float(w["end"])
            except (KeyError, TypeError, ValueError):
                gap = 0.0
        else:
            gap = 0.0
        sentence_end = text.endswith((".", "?", "!")) or text.endswith(("।",))
        if len(buf) >= max_words or sentence_end or gap >= 0.6:
            segments.append({
                "start": float(buf[0]["start"]),
                "end": float(buf[-1]["end"]),
                "text": " ".join(b.get("text", "") for b in buf),
            })
            buf = []
    if buf:
        segments.append({
            "start": float(buf[0]["start"]),
            "end": float(buf[-1]["end"]),
            "text": " ".join(b.get("text", "") for b in buf),
        })
    return segments


def transcribe_words_native(video_id: str, video_path: Path) -> dict:
    """Transcribes the spoken words exactly as spoken (STT, no translation).

    Auto-detects the language and preserves the speaker's original wording,
    including Hinglish code-switching (e.g. "Aaj we will discuss the new
    feature."). Used to feed the viral-clip finder and for native burn-in."""
    return _transcribe_words(video_id, video_path, language=None, task="transcribe")


def transcribe_words_english(video_id: str, video_path: Path) -> list:
    """Transcribes the original English speech directly via STT (no translation).

    Uses Whisper's 'transcribe' task with language='en' so subtitles come
    from what was actually spoken in English. Preserves the speaker's exact
    meaning/wording (only obvious transcription errors corrected by the
    model itself); names, technical terms, product names and acronyms are
    kept as spoken. Never translates non-English audio."""
    return _transcribe_words(video_id, video_path, language="en", task="transcribe")["words"]


def words_to_transcript_text(words: list, segments: list | None = None, silences: list | None = None) -> str:
    """Builds the structured transcript the viral-clip prompt expects.

    Prefers sentence segments with [start-end] ranges plus [pause Xs] markers
    so Gemini can cut on thought boundaries instead of mid-word. Falls back
    to the legacy 12-word [seconds] lines when only words are available
    (e.g. unit tests, very old callers)."""
    if segments:
        lines = []
        sil_idx = 0
        silences = silences or []
        prev_end = 0.0
        for seg in segments:
            try:
                s, e = float(seg["start"]), float(seg["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            # Attach pauses that fall between the previous segment end and this one.
            while sil_idx < len(silences):
                try:
                    ps, pe = float(silences[sil_idx]["start"]), float(silences[sil_idx]["end"])
                except (KeyError, TypeError, ValueError):
                    sil_idx += 1
                    continue
                if pe <= s:
                    if ps >= prev_end and pe - ps >= 0.3:
                        lines.append(f"[pause {pe - ps:.1f}s]")
                    sil_idx += 1
                else:
                    break
            lines.append(f"[{s:.1f}-{e:.1f}] {text}")
            prev_end = e
        return "\n".join(lines)

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
