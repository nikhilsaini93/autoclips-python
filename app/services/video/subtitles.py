import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(max(0.0, seconds) * 1000)))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def chunk_words_for_captions(words: list, max_words: int = 4, max_dur_sec: float = 1.8,
                             min_dur_sec: float = 0.9, max_chars: int = 42):
    """Sentence-aware caption chunks: at most max_words / max_chars /
    max_dur_sec per line, splitting early on sentence-ending punctuation
    so captions read naturally and never wrap to 3 lines on a 1080px crop.
    Chunks never overlap: each start is clamped to the previous end, and
    short lines are padded to min_dur_sec (capped by the next word start)
    for readability at ~160wpm."""
    chunks = []
    buf = []
    buf_chars = 0
    prev_end: float | None = None
    for w in words:
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if we <= ws:
            continue
        text = (w.get("text") or "")
        # Clamp to previous chunk so SRT times never overlap/flicker.
        if prev_end is not None:
            ws = max(ws, prev_end)
            if we <= ws:
                continue
        buf.append({"start": ws, "end": we, "text": text})
        buf_chars += len(text) + 1
        sentence_end = text.endswith((".", "?", "!", "…")) or text.endswith(("।",))
        dur = buf[-1]["end"] - buf[0]["start"] if buf else 0.0
        if len(buf) >= max_words or sentence_end or dur >= max_dur_sec or buf_chars >= max_chars:
            start = buf[0]["start"]
            end = max(buf[-1]["end"], start + min_dur_sec)
            # Don't hang a single short word: cap padding at natural end + 0.4s
            # when the line is far shorter than min_dur.
            if len(buf) == 1 and buf[-1]["end"] - buf[0]["start"] < 0.4:
                end = min(end, buf[-1]["end"] + 0.4)
            chunks.append((start, end, " ".join(b["text"] for b in buf)))
            prev_end = end
            buf = []
            buf_chars = 0
    if buf:
        start = buf[0]["start"] if prev_end is None else max(buf[0]["start"], prev_end)
        end = max(buf[-1]["end"], start + min(min_dur_sec, 1.2))
        if end > start:
            chunks.append((start, end, " ".join(b["text"] for b in buf)))
    # Final safety: enforce strictly increasing, non-overlapping times.
    fixed = []
    last_end = -1.0
    for s, e, t in chunks:
        s = max(s, last_end + 0.001) if fixed else s
        if e <= s:
            continue
        fixed.append((s, e, t))
        last_end = e
    return fixed


def write_srt(words: list, start_sec: float, end_sec: float, srt_path: Path) -> bool:
    """Writes an SRT with timestamps relative to start_sec, using only the
    words that fall inside [start_sec, end_sec]. Returns False if empty."""
    clip_duration = end_sec - start_sec
    clip_words = []
    for w in words:
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if we <= start_sec or ws >= end_sec:
            continue
        rel_start = max(0.0, ws - start_sec)
        rel_end = min(clip_duration, we - start_sec)
        # Drop sub-150ms edge slivers (word straddling the cut) — they render
        # as a 1-frame flash caption at head/tail.
        if rel_end - rel_start < 0.15:
            continue
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
