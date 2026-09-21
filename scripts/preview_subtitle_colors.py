#!/usr/bin/env python3
"""Colored-caption test — companion to preview_subtitle_fonts.py.

Same 3 dialogues rendered 3 ways (ASS inline colors, same ffmpeg pipeline):
  A. KEYWORD-YELLOW — important words (caps, numbers, money words) in yellow,
     rest white. Deterministic, based on the dialogue text (Hormozi style).
  B. RANDOM-POP — 1-2 random words per line in a random pop color
     (yellow/cyan/lime/pink). Seeded, reproducible.
  C. ALTERNATING-LINES — whole line alternates white / yellow per dialogue.

Usage:
    venv/bin/python scripts/preview_subtitle_colors.py

Outputs (storage/tmp/font_preview_colors/):
    color_comparison.mp4, color_sheet.jpg + per-variant clips.
Font is fixed to Anton (current recommendation) so COLOR is the only variable.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preview_subtitle_fonts import FONTS_DIR, LABEL_FONT, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "storage" / "tmp" / "font_preview_colors"

FONT = "Anton"
FONTSIZE = 92

# Same dialogues in every variant — fair comparison.
DIALOGUES = [
    (0.0, 3.0, "POVERTY to PROFIT in 12 months"),
    (3.0, 6.0, "Paise kamana hai? Watch this till END"),
    (6.0, 9.0, "I made 5 LAKH in 30 days"),
]

YELLOW = "&H0000FFFF&"
WHITE = "&H00FFFFFF&"
PALETTE = {
    "yellow": "&H0000FFFF&",
    "cyan": "&H00FFFF00&",
    "lime": "&H0000FF00&",
    "pink": "&H00FF00FF&",
}

MONEY_WORDS = {
    "profit", "paise", "paisa", "lakh", "lakhs", "crore", "crores",
    "rupees", "rupee", "money", "income", "salary", "rich", "poverty",
    "business", "profitable", "cash",
}


def is_keyword(word: str) -> bool:
    clean = word.strip("?!.,").strip()
    if not clean:
        return False
    if any(ch.isdigit() for ch in clean):
        return True
    if clean.isupper() and len(clean) > 1:
        return True
    return clean.lower() in MONEY_WORDS


def wrap_words(words: list[str], width: int = 16) -> list[list[str]]:
    """Greedy wrap for 1080-wide vertical video."""
    lines, cur, cur_len = [], [], 0
    for w in words:
        add = len(w) + (1 if cur else 0)
        if cur and cur_len + add > width:
            lines.append(cur)
            cur, cur_len = [w], len(w)
        else:
            cur.append(w)
            cur_len += add
    if cur:
        lines.append(cur)
    return lines


def paint(words: list[str], colors: list[str]) -> str:
    """Wrap words in ASS color tags, wrap lines with \\N, skip redundant tags."""
    out_words = []
    for w, c in zip(words, colors):
        out_words.append(f"{{\\c{c}}}{w}" if c != WHITE else w)
    lines = wrap_words(out_words)
    text = "\\N".join(" ".join(line) for line in lines)
    return text


def ass_time(sec: float) -> str:
    cs = int(round(sec * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def write_ass(path: Path, dialogue_colors: list[list[str]]) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{FONTSIZE},{WHITE},&H000000FF&,&H00000000,&H80000000&,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for (start, end, line), colors in zip(DIALOGUES, dialogue_colors):
            words = line.split()
            text = paint(words, colors)
            f.write(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{text}\n")


def variant_colors() -> dict[str, list[list[str]]]:
    words_list = [line.split() for _, _, line in DIALOGUES]

    a = [[YELLOW if is_keyword(w) else WHITE for w in words] for words in words_list]

    rng = random.Random(7)
    palette = list(PALETTE.values())
    b = []
    for words in words_list:
        cols = [WHITE] * len(words)
        for i in rng.sample(range(len(words)), k=min(2, len(words))):
            cols[i] = rng.choice(palette)
        b.append(cols)

    c = [[WHITE] * len(words_list[0]), [YELLOW] * len(words_list[1]), [WHITE] * len(words_list[2])]

    return {"A-keyword-yellow": a, "B-random-pop": b, "C-alternating-lines": c}


TITLES = {
    "A-keyword-yellow": "A. KEYWORD YELLOW (Hormozi style)",
    "B-random-pop": "B. RANDOM POP (random words/colors)",
    "C-alternating-lines": "C. ALTERNATING LINES (white/yellow)",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    variants = variant_colors()

    base = OUT / "_base.mp4"
    if not base.exists():
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", "color=c=0x1a1a2e:s=1080x1920:r=30:d=9",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             str(base)])

    for key, colors in variants.items():
        ass = OUT / f"_{key}.ass"
        write_ass(ass, colors)
        esc = str(ass).replace("\\", "/").replace(":", "\\:")
        vf = (f"drawtext=fontfile={LABEL_FONT}:text='{TITLES[key]}':"
              f"fontsize=40:fontcolor=white:borderw=2:bordercolor=black:"
              f"x=(w-text_w)/2:y=200,"
              f"subtitles='{esc}':fontsdir='{FONTS_DIR}'")
        print(f"Rendering {key} ...")
        run(["ffmpeg", "-y", "-i", str(base), "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(OUT / f"{key}.mp4")])

    lst = OUT / "_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for key in variants:
            f.write(f"file '{OUT / (key + '.mp4')}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(OUT / "color_comparison.mp4")])

    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        thumbs = []
        for key in variants:
            fp = OUT / f"_{key}.jpg"
            # One frame per dialogue line, stacked side-by-side per variant.
            frames = []
            for ss in ("1.5", "4.5", "7.5"):
                run(["ffmpeg", "-y", "-ss", ss, "-i", str(OUT / f"{key}.mp4"),
                     "-vframes", "1", "-q:v", "3", str(fp)])
                img = cv2.imread(str(fp))
                frames.append(cv2.resize(img, (360, 640)))
            row = np.hstack(frames)
            bar = np.zeros((52, 360 * 3, 3), np.uint8)
            cv2.putText(bar, TITLES[key][:52], (12, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                        cv2.LINE_AA)
            thumbs.append(np.vstack([bar, row]))
            fp.unlink(missing_ok=True)
        sheet = np.vstack(thumbs)
        cv2.imwrite(str(OUT / "color_sheet.jpg"), sheet,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        print("Wrote color_sheet.jpg")
    except ImportError:
        print("[WARN] cv2 not importable — skipping color_sheet.jpg")

    for p in list(OUT.glob("_*.ass")) + [lst, base]:
        p.unlink(missing_ok=True)
    print(f"\nDONE. Watch: {OUT / 'color_comparison.mp4'}")
    print("Reply A, B or C (and font number) and I'll apply both to render.py.")


if __name__ == "__main__":
    main()
