#!/usr/bin/env python3
"""Render a subtitle-font comparison test so you can SEE each option burned
into a real 1080x1920 clip (same ffmpeg libass pipeline as render.py).

Usage:
    venv/bin/python scripts/preview_subtitle_fonts.py
    venv/bin/python scripts/preview_subtitle_fonts.py --skip-download
    venv/bin/python scripts/preview_subtitle_fonts.py --out storage/tmp/font_preview

Outputs (default: storage/tmp/font_preview/):
    01-dejavu-baseline.mp4  ...  08-inter-xbold.mp4  (3s each, 1080x1920)
    font_comparison.mp4     (all styles back-to-back, watch this one file)
    preview_sheet.jpg       (one stacked image, quick glance)
    styles.txt              (exact force_style string per option, for apply step)

All styles keep the SAME geometry/position as production
(Alignment=2 bottom-center, MarginV=60) — only the font changes,
plus a small per-font size compensation so the comparison is fair.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "assets" / "fonts"
DEFAULT_OUT = ROOT / "storage" / "tmp" / "font_preview"
LABEL_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Authoritative static TTFs straight from Google Fonts (correct name tables).
# (slug, direct gstatic TTF url, filename)
FONTS_TO_FETCH = [
    ("anton", "https://fonts.gstatic.com/s/anton/v27/1Ptgg87LROyAm0K0.ttf", "anton-400.ttf"),
    ("bebas", "https://fonts.gstatic.com/s/bebasneue/v16/JTUSjIg69CK48gW7PXooxW4.ttf", "bebas-neue-400.ttf"),
    ("montserrat", "https://fonts.gstatic.com/s/montserrat/v31/JTUHjIg1_i6t8kCHKm4532VJOt5-QNFgpCvr70w-.ttf", "montserrat-800.ttf"),
    ("poppins", "https://fonts.gstatic.com/s/poppins/v24/pxiByp8kv8JHgFVrLCz7V1s.ttf", "poppins-700.ttf"),
    ("archivo", "https://fonts.gstatic.com/s/archivoblack/v23/HTxqL289NzCGg4MzN6KJ7eW6OYs.ttf", "archivo-black-400.ttf"),
    ("oswald", "https://fonts.gstatic.com/s/oswald/v57/TK3_WkUHHAIjg75cFRf3bXL8LICs1xZogUE.ttf", "oswald-700.ttf"),
    ("inter", "https://fonts.gstatic.com/s/inter/v20/UcCO3FwrK3iLTeHuS_nVMrMxCp50SjIw2boKoduKmMEVuDyYMZg.ttf", "inter-800.ttf"),
]

# Fallback FontName if fc-query is unavailable (family names of the files above).
FALLBACK_FAMILY = {
    "anton": "Anton",
    "bebas": "Bebas Neue",
    "montserrat": "Montserrat",
    "poppins": "Poppins",
    "archivo": "Archivo Black",
    "oswald": "Oswald",
    "inter": "Inter",
}

# Sample captions burned into every test clip (same 3 lines each time).
SRT_LINES = [
    (0.0, 1.0, "This is how your captions will look"),
    (1.0, 2.0, "POVERTY to PROFIT in 12 months"),
    (2.0, 3.0, "Paise kamana hai? Watch this!"),
]

BASE_COMMON = (
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=60"
)


def style_defs(family: dict[str, str]) -> list[dict]:
    """8 options. Only FontName/FontSize/Bold vary — fair comparison."""
    return [
        {
            "key": "01-dejavu-baseline",
            "title": "1. DejaVu Sans (CURRENT)",
            "desc": "Current production style — neutral, a bit thin/plain.",
            "font": "DejaVu Sans",
            "size": 15,
            "bold": 1,
        },
        {
            "key": "02-anton",
            "title": "2. ANTON (Hormozi style) *RECOMMENDED*",
            "desc": "Tall condensed punch. The classic viral finance/motivation look.",
            "font": family["anton"],
            "size": 16,
            "bold": 1,
        },
        {
            "key": "03-bebas-neue",
            "title": "3. BEBAS NEUE",
            "desc": "Tall clean caps, lighter than Anton. Minimal/premium feel.",
            "font": family["bebas"],
            "size": 17,
            "bold": 1,
        },
        {
            "key": "04-montserrat-xbold",
            "title": "4. MONTSERRAT EXTRABOLD",
            "desc": "Modern geometric. Very readable, safe for Hindi+Hinglish mix.",
            "font": family["montserrat"],
            "size": 15,
            "bold": 0,
        },
        {
            "key": "05-poppins-bold",
            "title": "5. POPPINS BOLD",
            "desc": "Friendly rounded. Great readability at small sizes.",
            "font": family["poppins"],
            "size": 15,
            "bold": 0,
        },
        {
            "key": "06-archivo-black",
            "title": "6. ARCHIVO BLACK (meme heavy)",
            "desc": "Extra-heavy meme style. Loud; can feel shouty for finance.",
            "font": family["archivo"],
            "size": 14,
            "bold": 0,
        },
        {
            "key": "07-oswald-bold",
            "title": "7. OSWALD BOLD",
            "desc": "Condensed like Anton but lighter. Good middle ground.",
            "font": family["oswald"],
            "size": 16,
            "bold": 0,
        },
        {
            "key": "08-inter-xbold",
            "title": "8. INTER EXTRABOLD",
            "desc": "Neutral UI font. Cleanest, least 'viral' personality.",
            "font": family["inter"],
            "size": 15,
            "bold": 0,
        },
    ]


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}\n{r.stderr[-3000:]}", file=sys.stderr)
        raise SystemExit(1)
    return r.stderr


def check_font_match(stderr: str, font: str, key: str) -> None:
    for line in stderr.splitlines():
        low = line.lower()
        if "no usable font" in low or "failed" in low or "error" in low:
            print(f"  [WARN:{key}] libass font issue for '{font}': {line.strip()}")
            return
    if f"-> {font.split()[0]}" not in stderr and "fontselect" in stderr:
        # fontselect ran but resolved somewhere unexpected — show it.
        for line in stderr.splitlines():
            if "fontselect" in line:
                print(f"  [INFO:{key}] {line.strip()}")


def fetch_fonts() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for slug, url, fname in FONTS_TO_FETCH:
        dest = FONTS_DIR / fname
        if dest.exists() and dest.stat().st_size > 10_000:
            # Re-download once if this is a known-bad fontsource build
            # (mislabelled family e.g. "Montserrat Thin ExtraBold").
            q = shutil.which("fc-query")
            bad = False
            if q and slug in ("montserrat", "inter"):
                try:
                    r = subprocess.run(
                        [q, "--format=%{family}\n", str(dest)],
                        capture_output=True, text=True, timeout=15,
                    )
                    if "Thin" in r.stdout or "," in r.stdout:
                        bad = True
                except Exception:  # noqa: BLE001
                    pass
            if not bad:
                print(f"  [ok] {fname} (cached)")
                continue
            print(f"  [fix] {fname} has bad name table, re-downloading")
        print(f"  [dl] {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] could not download {fname}: {e}")
            if dest.exists():
                dest.unlink()


def detect_family(slug: str, fname: str) -> str:
    """Ask fontconfig for the real family name libass expects.

    Prefers the most specific (usually last/typographic) family entry.
    """
    path = FONTS_DIR / fname
    if path.exists():
        q = shutil.which("fc-query")
        if q:
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
    return FALLBACK_FAMILY[slug]


def write_srt(path: Path) -> None:
    def ts(s: float) -> str:
        ms = int(round(s * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        sec, milli = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{milli:03d}"

    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, text) in enumerate(SRT_LINES, start=1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n\n")


def force_style(font: str, size: int, bold: int) -> str:
    return f"FontName={font},FontSize={size},Bold={bold},{BASE_COMMON}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Render subtitle font comparison test.")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH")

    if not args.skip_download:
        print("Downloading fonts to assets/fonts/ ...")
        fetch_fonts()

    family = {slug: detect_family(slug, fname) for slug, _, fname in FONTS_TO_FETCH}
    print("Detected FontNames:", family)
    styles = style_defs(family)

    # 1) Dark 1080x1920 base clip, 3s (matches vertical production output).
    base = out / "_base.mp4"
    if not base.exists():
        print("Generating base clip ...")
        run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "color=c=0x1a1a2e:s=1080x1920:r=30:d=3",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(base),
        ])

    # 2) Per-style clips: header label (drawtext) + sample captions (libass).
    for st in styles:
        srt = out / f"_{st['key']}.srt"
        write_srt(srt)
        esc_srt = str(srt).replace("\\", "/").replace(":", "\\:")
        fontsdir = str(FONTS_DIR)
        fs = force_style(st["font"], st["size"], st["bold"])
        vf = (
            f"drawtext=fontfile={LABEL_FONT}:text='{st['title']}':"
            f"fontsize=44:fontcolor=white:borderw=2:bordercolor=black:"
            f"x=(w-text_w)/2:y=220,"
            f"subtitles='{esc_srt}':fontsdir='{fontsdir}':force_style='{fs}'"
        )
        target = out / f"{st['key']}.mp4"
        print(f"Rendering {st['key']}  (FontName={st['font']}) ...")
        stderr = run([
            "ffmpeg", "-y", "-i", str(base),
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(target),
        ])
        check_font_match(stderr, st["font"], st["key"])

    # 3) Concatenated comparison video (watch this one file to decide).
    lst = out / "_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for st in styles:
            f.write(f"file '{out / (st['key'] + '.mp4')}'\n")
    comp = out / "font_comparison.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(comp)])

    # 4) Contact sheet: middle frame of each clip, stacked vertically.
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        thumbs = []
        for st in styles:
            frame_p = out / f"_{st['key']}.jpg"
            run(["ffmpeg", "-y", "-ss", "1.5", "-i",
                 str(out / f"{st['key']}.mp4"),
                 "-vframes", "1", "-q:v", "3", str(frame_p)])
            img = cv2.imread(str(frame_p))
            img = cv2.resize(img, (540, 960))
            bar = np.zeros((52, 540, 3), np.uint8)
            cv2.putText(bar, st["title"][:44], (12, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                        cv2.LINE_AA)
            thumbs.append(np.vstack([bar, img]))
        sheet = np.hstack([
            np.vstack(thumbs[:4]),
            np.vstack(thumbs[4:]),
        ])
        cv2.imwrite(str(out / "preview_sheet.jpg"), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        print("Wrote preview_sheet.jpg")
    except ImportError:
        print("[WARN] cv2/numpy not importable — skipping preview_sheet.jpg")

    # 5) Exact style strings for the apply step.
    with open(out / "styles.txt", "w", encoding="utf-8") as f:
        for st in styles:
            f.write(f"[{st['key']}] {st['title']}\n")
            f.write(f"  {st['desc']}\n")
            f.write(f"  force_style='{force_style(st['font'], st['size'], st['bold'])}'\n\n")

    for p in list(out.glob("_*.srt")) + [lst]:
        p.unlink(missing_ok=True)
    print(f"\nDONE. Watch: {comp}")
    print(f"Per-style clips + preview_sheet.jpg in: {out}")
    print("Reply with the number you like (e.g. '2') and I'll apply it to render.py.")


if __name__ == "__main__":
    main()
