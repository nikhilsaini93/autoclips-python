import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(max(0.0, seconds) * 1000)))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def chunk_words_for_captions(words: list, max_words: int = 4, max_dur_sec: float = 1.6,
                             min_dur_sec: float = 0.6):
    """Sentence-aware caption chunks: at most max_words / max_dur_sec per line,
    splitting early on sentence-ending punctuation so captions read naturally.
    Short lines are padded to min_dur_sec for readability."""
    chunks = []
    buf = []
    for w in words:
        buf.append(w)
        text = (w.get("text") or "")
        sentence_end = text.endswith((".", "?", "!")) or text.endswith(("।",))
        dur = buf[-1]["end"] - buf[0]["start"] if buf else 0.0
        if len(buf) >= max_words or sentence_end or dur >= max_dur_sec:
            start = buf[0]["start"]
            end = max(buf[-1]["end"], start + min_dur_sec)
            chunks.append((start, end, " ".join(b["text"] for b in buf)))
            buf = []
    if buf:
        start = buf[0]["start"]
        end = max(buf[-1]["end"], start + min_dur_sec)
        chunks.append((start, end, " ".join(b["text"] for b in buf)))
    return chunks


def write_srt(words: list, start_sec: float, end_sec: float, srt_path: Path) -> bool:
    """Writes an SRT with timestamps relative to start_sec, using only the
    words that fall inside [start_sec, end_sec]. Returns False if empty."""
    clip_duration = end_sec - start_sec
    clip_words = []
    for w in words:
        if w["end"] <= start_sec or w["start"] >= end_sec:
            continue
        rel_start = max(0.0, w["start"] - start_sec)
        rel_end = min(clip_duration, w["end"] - start_sec)
        if rel_end <= rel_start:
            continue
        clip_words.append({"start": rel_start, "end": rel_end, "text": w["text"]})

    if not clip_words:
        logger.warning("No words fall inside [%.1f, %.1f] - not writing %s", start_sec, end_sec, srt_path)
        return False

    chunks = chunk_words_for_captions(clip_words)
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (rel_start, rel_end, text) in enumerate(chunks, start=1):
            f.write(f"{i}\n{format_srt_timestamp(rel_start)} --> {format_srt_timestamp(rel_end)}\n{text}\n\n")
    logger.info("Wrote %s (%d caption lines)", srt_path, len(chunks))
    return True
