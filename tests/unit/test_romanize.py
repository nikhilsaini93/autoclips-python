"""Tests for Hinglish subtitles (Devanagari -> Latin romanization).

Expected pairs were verified against indic-transliteration 2.3.82 output
plus the Hinglish cleanup layer (schwa deletion, final-A shortening,
nukta/anusvara mapping).
"""

from app.services.video.romanize import hinglish_word, to_hinglish_words


def test_hindi_words_become_hinglish():
    pairs = {
        "शादी": "shaadi",
        "ख़त्म": "khatm",
        "रही": "rahi",
        "है": "hai",
        "क्या": "kya",
        "पैसा": "paisa",
        "सब": "sab",
        "कुझ": "kujh",
        "कुछ": "kuchh",
        "नहीं": "nahin",
        "सास": "saas",
        "क्यों": "kyon",
        "पसंद": "pasand",
        "बहू": "bahu",
        "हूँ": "hun",
        "हैं": "hain",
        "मैं": "main",
        "माँ": "maan",
        "गाँव": "gaanv",
        "होता": "hota",
        "ज़्यादा": "zyaada",
        "ग़म": "gham",
    }
    for deva, expected in pairs.items():
        assert hinglish_word(deva) == expected, deva


def test_english_and_other_text_untouched():
    for text in ["Hello", "future", "Successful", "COVID19", "01:20", "", "123"]:
        assert hinglish_word(text) == text


def test_mixed_script_word():
    assert hinglish_word("COVID19बहू") == "COVID19bahu"


def test_to_hinglish_words_keeps_timings_and_shape():
    words = [
        {"start": 1.0, "end": 1.5, "text": "शादी"},
        {"start": 1.5, "end": 2.0, "text": "khatm"},
        {"start": 2.0, "end": 2.2, "text": "है?"},
    ]
    out = to_hinglish_words(words)
    assert [w["text"] for w in out] == ["shaadi", "khatm", "hai?"]
    assert [(w["start"], w["end"]) for w in out] == [(1.0, 1.5), (1.5, 2.0), (2.0, 2.2)]
    # input not mutated
    assert words[0]["text"] == "शादी"


def test_to_hinglish_words_tolerates_garbage():
    assert to_hinglish_words(None) == []
    assert to_hinglish_words([]) == []
    out = to_hinglish_words([None, "x", {"nope": 1}])
    assert out[0] is None and out[1] == "x" and out[2]["text"] == ""


def test_resolve_subtitle_words_hinglish_branch(monkeypatch):
    import app.services.viral as viral_mod

    native = {"words": [{"start": 0.0, "end": 1.0, "text": "शादी"}]}
    monkeypatch.setattr(
        viral_mod, "transcribe_words_native", lambda *a: native
    )
    out = viral_mod.resolve_subtitle_words("vid", "/tmp/v.mp4", "hinglish")
    assert [w["text"] for w in out] == ["shaadi"]


def test_hinglish_is_default_subtitle_mode():
    from app.api.schemas import ClipRequest, ViralClipsRequest
    from app.services.viral import subtitle_label

    assert ViralClipsRequest(url="https://youtu.be/x").subtitles == "hinglish"
    assert ClipRequest(url="https://youtu.be/x", start="0", end="1").subtitles == "hinglish"
    assert subtitle_label("hinglish") == "Hinglish"
