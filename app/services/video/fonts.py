"""Font-coverage checks for burned-in subtitles.

The ASS caption style uses DejaVu Sans, which has no Devanagari glyphs.
libass falls back per-glyph via fontconfig, but only when *some* installed
font covers the script — otherwise Hindi/Marathi captions render blank and
the clip looks like subtitles "never show up". These helpers detect that
situation so we can warn loudly instead of shipping silent videos.
"""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def contains_devanagari(text: str) -> bool:
    """True when text has any Devanagari codepoint (U+0900-U+097F)."""
    try:
        return any("\u0900" <= ch <= "\u097f" for ch in (text or ""))
    except TypeError:
        return False


def has_devanagari_font() -> bool:
    """True when fontconfig can serve Devanagari (e.g. Noto Sans Devanagari).

    No result cache on purpose: fonts are often installed mid-session
    (apt in a notebook) and /health + render must reflect that. fc-list
    takes ~0.1s, cheap enough per clip / health poll.
    """
    if not shutil.which("fc-list"):
        return False
    try:
        out = subprocess.run(
            ["fc-list", ":lang=hi", "family"],
            capture_output=True, text=True, timeout=15,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        logger.debug("fc-list devanagari check failed", exc_info=True)
        return False


def warn_if_glyphs_missing(words: list | None) -> None:
    """Log a loud warning when burned-in captions need glyphs no font has."""
    try:
        texts = [
            (w.get("text") if isinstance(w, dict) else "") or ""
            for w in (words or [])
        ]
    except Exception:
        return
    if any(contains_devanagari(t) for t in texts) and not has_devanagari_font():
        logger.warning(
            "Captions contain Devanagari (Hindi) but no Devanagari font is "
            "installed — burned-in subs will render BLANK. Install "
            "fonts-noto-core (sudo apt install fonts-noto-core) and "
            "re-render. /health reports this as devanagari_font=false."
        )
