"""Devanagari -> Hinglish romanization for burned-in subtitles.

`subtitles=native` burns Devanagari script, which needs a Devanagari font
and which this channel's audience doesn't read on Shorts. `hinglish` keeps
the *spoken* words (same timings as native) but renders them in Latin
script: English audio passes through untouched, Hindi audio becomes readable
Hinglish (शादी -> shaadi, नहीं -> nahin).

Raw ITRANS is not Hinglish (shAdI Katma, nahIM), so a cleanup layer maps it
to natural Hinglish conventions: schwa deletion (saas, khatm), A->aa
medially but ->a finally (shaadi vs kya), nukta capitals (K->kh, Z->z),
anusvara M->n. English/Latin words are never touched.
"""

import logging
import re

logger = logging.getLogger(__name__)

_DEVA_RUN = re.compile(r"[\u0900-\u097f]+")

# Longest-first: digraphs before their single-letter prefixes (Dh before D,
# Th before T, Ch before C, Sh before S, ~N before N).
_ITRANS_FIXES = [
    ("kSh", "ksh"), ("j~n", "gy"), ("j~N", "gy"),
    ("Dh", "dh"), ("Th", "th"), ("Ch", "chh"), ("Sh", "sh"), ("Jh", "jh"),
    ("~n", "n"), ("~N", "n"), (".n", "n"), (".N", "n"),
    (".d", "d"), (".D", "dh"), (".t", "t"),
    ("K", "kh"), ("G", "gh"), ("Z", "z"),
    ("D", "d"), ("T", "t"), ("N", "n"),
    ("M", "n"), ("H", "h"), ("R", "ri"), ("L", "li"),
    ("A", "aa"), ("I", "i"), ("U", "u"),
]

_transliterate = None
_no_dep_warned = False


def _get_transliterate():
    """Lazy indic-transliteration import (slow first import, ~1s of tables)."""
    global _transliterate, _no_dep_warned
    if _transliterate is None and not _no_dep_warned:
        try:
            from indic_transliteration import sanscript
            from indic_transliteration.sanscript import transliterate

            def _t(text: str) -> str:
                return transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)

            _transliterate = _t
        except ImportError:
            _no_dep_warned = True
            logger.warning(
                "indic-transliteration not installed — 'hinglish' subtitles "
                "fall back to native script. pip install -r requirements.txt"
            )
    return _transliterate


def _clean_itrans(token: str) -> str:
    # Word-final long आ is written short in Hinglish (क्या -> kya, होता ->
    # hota) while medial आ stays long (शादी -> shaadi).
    if token.endswith("A"):
        token = token[:-1] + "a"
        converted_final = True
    else:
        converted_final = False
    # Schwa deletion on the ITRANS form: a *lowercase* final 'a' can only be
    # the inherent schwa (long आ is 'A'), so सास (sAsa) -> saas. Skipped
    # when the ending came from the rule above (क्या -> kya, not ky).
    if not converted_final and len(token) > 1 and token.endswith("a"):
        token = token[:-1]
    for src, dst in _ITRANS_FIXES:
        if src in token:
            token = token.replace(src, dst)
    # Danda (।) survives transliteration as '|' — drop it so captions don't
    # print a stray bar (sentence chunking then falls back to length rules).
    return token.replace("|", "")


def hinglish_word(word: str) -> str:
    """Romanizes Devanagari runs inside one word; Latin text untouched."""
    if not word or not _DEVA_RUN.search(word):
        return word
    func = _get_transliterate()
    if func is None:
        return word
    try:
        return "".join(
            _clean_itrans(func(part)) if _DEVA_RUN.fullmatch(part) else part
            for part in re.split(r"([\u0900-\u097f]+)", word)
        )
    except Exception:
        logger.debug("Hinglish romanization failed for %r", word, exc_info=True)
        return word


def to_hinglish_words(words: list | None) -> list:
    """Copies a Whisper word list with each text romanized (timings kept)."""
    out = []
    for w in words or []:
        if not isinstance(w, dict):
            out.append(w)
            continue
        w = dict(w)
        try:
            w["text"] = hinglish_word(w.get("text") or "")
        except Exception:
            pass
        out.append(w)
    return out
