"""Tests for Devanagari font-coverage checks (production 2026-09-18).

Hindi clips rendered "without subtitles": the ASS style uses DejaVu Sans,
which has no Devanagari glyphs, and no Devanagari font was installed — so
libass burned blank lines. fonts.warn_if_glyphs_missing must flag exactly
that situation and stay silent otherwise.
"""

from app.services.video.fonts import (
    contains_devanagari,
    has_devanagari_font,
    warn_if_glyphs_missing,
)


def test_contains_devanagari():
    assert contains_devanagari("Shaadi Khatam") is False
    assert contains_devanagari("शादी ख़त्म") is True
    assert contains_devanagari("mixed शादी text") is True
    assert contains_devanagari("") is False
    assert contains_devanagari(None) is False


def test_has_devanagari_font_returns_bool():
    # Environment-dependent (CI runners may or may not have Noto installed);
    # only the contract matters: bool, never raises.
    assert isinstance(has_devanagari_font(), bool)


def test_warn_if_glyphs_missing_flags_hindi_without_font(monkeypatch, caplog):
    import app.services.video.fonts as fonts_mod

    monkeypatch.setattr(fonts_mod, "has_devanagari_font", lambda: False)
    words = [{"start": 0.0, "end": 1.0, "text": "शादी ख़त्म हो रही है"}]
    with caplog.at_level("WARNING", logger="app.services.video.fonts"):
        warn_if_glyphs_missing(words)
    assert any("BLANK" in r.message for r in caplog.records)


def test_warn_if_glyphs_missing_silent_for_latin(monkeypatch, caplog):
    import app.services.video.fonts as fonts_mod

    monkeypatch.setattr(fonts_mod, "has_devanagari_font", lambda: False)
    words = [{"start": 0.0, "end": 1.0, "text": "hello world"}]
    with caplog.at_level("WARNING", logger="app.services.video.fonts"):
        warn_if_glyphs_missing(words)
    assert not caplog.records


def test_warn_if_glyphs_missing_silent_when_font_present(monkeypatch, caplog):
    import app.services.video.fonts as fonts_mod

    monkeypatch.setattr(fonts_mod, "has_devanagari_font", lambda: True)
    words = [{"start": 0.0, "end": 1.0, "text": "शादी ख़त्म हो रही है"}]
    with caplog.at_level("WARNING", logger="app.services.video.fonts"):
        warn_if_glyphs_missing(words)
    assert not caplog.records


def test_warn_if_glyphs_missing_tolerates_garbage():
    warn_if_glyphs_missing(None)
    warn_if_glyphs_missing([])
    warn_if_glyphs_missing([None, "x", {"nope": 1}])
