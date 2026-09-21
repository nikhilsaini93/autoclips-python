import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)

# Production subtitle style — chosen by user:
#   Font: Poppins Bold Italic (true italic face in assets/fonts/)
#   Color: random-pop (1-2 random words per caption in a pop color)
SUBTITLE_FONT = "Poppins"
SUBTITLE_FONTSIZE = 72
SUBTITLE_BOLD = -1  # ASS: -1 = true → selects Bold face
SUBTITLE_ITALIC = -1  # ASS: -1 = true → selects true Italic face

WHITE = "&H00FFFFFF&"
POP_PALETTE = (
    "&H0000FFFF&",  # yellow
    "&H00FFFF00&",  # cyan
    "&H0000FF00&",  # lime
    "&H00FF00FF&",  # pink
)


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


def format_ass_timestamp(seconds: float) -> str:
    total_cs = max(0, int(round(max(0.0, seconds) * 100)))
    hours, rem = divmod(total_cs, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _wrap_colored_words(colored: list[str], width: int = 14) -> str:
    """Greedy wrap (plain-text width) then join visual lines with ASS \\N."""
    lines, cur, cur_len = [], [], 0
    for token in colored:
        plain = token.split("}")[-1] if "}" in token else token
        add = len(plain) + (1 if cur else 0)
        if cur and cur_len + add > width:
            lines.append(cur)
            cur, cur_len = [token], len(plain)
        else:
            cur.append(token)
            cur_len += add
    if cur:
        lines.append(cur)
    return "\\N".join(" ".join(line) for line in lines)


def write_ass(
    words: list,
    start_sec: float,
    end_sec: float,
    ass_path: Path,
    seed: int | None = None,
    font: str = SUBTITLE_FONT,
    fontsize: int = SUBTITLE_FONTSIZE,
) -> bool:
    """Writes an ASS with Poppins Bold Italic + random-pop word colors.

    Same chunking as write_srt, timestamps relative to start_sec.
    Each caption gets 1-2 random words painted in a random pop color
    (yellow/cyan/lime/pink), rest white. Returns False if empty.

    seed: for reproducible tests. None → random per render (seeded from
    time + pid so concurrent renders don't repeat the same colors).
    """
    import os
    import time

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
        logger.warning(
            "No words fall inside [%.1f, %.1f] - not writing %s",
            start_sec, end_sec, ass_path,
        )
        return False

    if seed is None:
        seed = (int(time.time() * 1000) ^ os.getpid()) & 0xFFFFFFFF

    chunks = chunk_words_for_captions(clip_words)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{fontsize},{WHITE},&H000000FF&,&H00000000&,"
        f"&H80000000&,{SUBTITLE_BOLD},{SUBTITLE_ITALIC},0,0,100,100,0,0,"
        "1,3,1,2,40,40,140,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for idx, (rel_start, rel_end, text) in enumerate(chunks):
            tokens = text.split()
            rng = random.Random((seed + idx * 0x9E3779B1) & 0xFFFFFFFF)
            colors = [WHITE] * len(tokens)
            for i in rng.sample(range(len(tokens)), k=min(2, len(tokens))):
                colors[i] = rng.choice(POP_PALETTE)
            colored = [
                f"{{\\c{c}}}{tok}" if c != WHITE else tok
                for tok, c in zip(tokens, colors)
            ]
            ass_text = _wrap_colored_words(colored)
            f.write(
                f"Dialogue: 0,{format_ass_timestamp(rel_start)},"
                f"{format_ass_timestamp(rel_end)},Cap,,0,0,0,,{ass_text}\n"
            )
    logger.info("Wrote %s (%d caption lines, random-pop)", ass_path, len(chunks))
    return True
