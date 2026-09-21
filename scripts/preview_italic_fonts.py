#!/usr/bin/env python3
"""Italic subtitle-font test — companion to preview_subtitle_fonts.py.

Only 3 of our shortlist families ship true italics (Montserrat, Poppins,
Inter). Oswald/Anton/Bebas/Archivo are upright-only, so for Anton we show
libass-synthesized faux-italic (Italic=1 with no italic face) for comparison.

Usage:
    venv/bin/python scripts/preview_italic_fonts.py

Outputs (storage/tmp/font_preview_italic/):
    italic_comparison.mp4, italic_sheet.jpg + per-style clips.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preview_subtitle_fonts import (  # noqa: E402
    BASE_COMMON,
    FONTS_DIR,
    LABEL_FONT,
    run,
    write_srt,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "storage" / "tmp" / "font_preview_italic"

ITALIC_FONTS = [
    ("montserrat-italic", "https://fonts.gstatic.com/s/montserrat/v31/JTUFjIg1_i6t8kCHKm459Wx7xQYXK0vOoz6jqyR6aX8.ttf", "montserrat-800italic.ttf"),
    ("poppins-italic", "https://fonts.gstatic.com/s/poppins/v24/pxiDyp8kv8JHgFVrJJLmy15lEA.ttf", "poppins-700italic.ttf"),
    ("inter-italic", "https://fonts.gstatic.com/s/inter/v20/UcCM3FwrK3iLTcvneQg7Ca725JhhKnNqk4j1ebLhAm8SrXTcWdxhjQ.ttf", "inter-800italic.ttf"),
]

FALLBACK = {
    "montserrat-italic": "Montserrat ExtraBold",
    "poppins-italic": "Poppins",
    "inter-italic": "Inter ExtraBold",
}


def detect(fname: str, slug: str) -> str:
    import shutil

    path = FONTS_DIR / fname
    q = shutil.which("fc-query")
    if q and path.exists():
        try:
            r = subprocess.run(
                [q, "--format=%{family}\n", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            entries = [e.strip() for e in r.stdout.strip().split(",") if e.strip()]
            if entries:
                return max(entries, key=len)
        except Exception:  # noqa: BLE001
            pass
    return FALLBACK[slug]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Downloading italic faces ...")
    for slug, url, fname in ITALIC_FONTS:
        dest = FONTS_DIR / fname
        if not (dest.exists() and dest.stat().st_size > 10_000):
            req = urllib.request.Request(url, headers={"User-Agent": "curl"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                import shutil as _sh

                _sh.copyfileobj(resp, f)
            print(f"  [dl] {fname}")
        else:
            print(f"  [ok] {fname} (cached)")

    fam = {slug: detect(fname, slug) for slug, _, fname in ITALIC_FONTS}
    print("Detected FontNames:", fam)

    styles = [
        {"key": "09-montserrat-italic", "title": "9. MONTSERRAT XBold ITALIC (true)",
         "font": fam["montserrat-italic"], "size": 15, "bold": 0, "italic": 1},
        {"key": "10-poppins-italic", "title": "10. POPPINS BOLD ITALIC (true)",
         "font": fam["poppins-italic"], "size": 15, "bold": 0, "italic": 1},
        {"key": "11-inter-italic", "title": "11. INTER XBold ITALIC (true)",
         "font": fam["inter-italic"], "size": 15, "bold": 0, "italic": 1},
        {"key": "12-anton-fauxitalic", "title": "12. ANTON FAUX-ITALIC (synthesized)",
         "font": "Anton", "size": 16, "bold": 1, "italic": 1},
    ]

    base = OUT / "_base.mp4"
    if not base.exists():
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", "color=c=0x1a1a2e:s=1080x1920:r=30:d=3",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             str(base)])

    for st in styles:
        srt = OUT / f"_{st['key']}.srt"
        write_srt(srt)
        esc = str(srt).replace("\\", "/").replace(":", "\\:")
        fs = (f"FontName={st['font']},FontSize={st['size']},Bold={st['bold']},"
              f"Italic={st['italic']},{BASE_COMMON}")
        vf = (f"drawtext=fontfile={LABEL_FONT}:text='{st['title']}':"
              f"fontsize=44:fontcolor=white:borderw=2:bordercolor=black:"
              f"x=(w-text_w)/2:y=220,"
              f"subtitles='{esc}':fontsdir='{FONTS_DIR}':force_style='{fs}'")
        print(f"Rendering {st['key']} (FontName={st['font']}) ...")
        run(["ffmpeg", "-y", "-i", str(base), "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(OUT / f"{st['key']}.mp4")])

    lst = OUT / "_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for st in styles:
            f.write(f"file '{OUT / (st['key'] + '.mp4')}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(OUT / "italic_comparison.mp4")])

    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        thumbs = []
        for st in styles:
            fp = OUT / f"_{st['key']}.jpg"
            run(["ffmpeg", "-y", "-ss", "1.5", "-i",
                 str(OUT / f"{st['key']}.mp4"),
                 "-vframes", "1", "-q:v", "3", str(fp)])
            img = cv2.imread(str(fp))
            img = cv2.resize(img, (540, 960))
            bar = np.zeros((52, 540, 3), np.uint8)
            cv2.putText(bar, st["title"][:44], (12, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                        cv2.LINE_AA)
            thumbs.append(np.vstack([bar, img]))
            fp.unlink(missing_ok=True)
        sheet = np.vstack(thumbs)
        cv2.imwrite(str(OUT / "italic_sheet.jpg"), sheet,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        print("Wrote italic_sheet.jpg")
    except ImportError:
        print("[WARN] cv2 not importable — skipping italic_sheet.jpg")

    for p in list(OUT.glob("_*.srt")) + [lst, base]:
        p.unlink(missing_ok=True)
    print(f"\nDONE. Watch: {OUT / 'italic_comparison.mp4'}")


if __name__ == "__main__":
    main()
