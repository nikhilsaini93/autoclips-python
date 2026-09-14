import json


def test_find_viral_clips_parsing(monkeypatch):
    import app.services.video.gemini as gemini

    class FakeModels:
        def generate_content(self, model, contents):
            class Resp:
                text = '```json\n[{"title":"T","start":"00:01:20","end":"00:01:55","score":95,"reason":"hook"}]\n```'
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(gemini, "get_genai_client", lambda: FakeClient())
    clips = gemini.find_viral_clips("dummy transcript")
    assert len(clips) == 1
    assert clips[0]["title"] == "T"
    # Old-format replies without the YT pack get normalized defaults.
    assert clips[0]["hashtags"] == []
    assert clips[0]["description"] == ""


def test_find_viral_clips_title_pack_hindi(monkeypatch):
    import app.services.video.gemini as gemini

    payload = json.dumps([{
        "title": "सफलता का एक मंत्र",
        "start": "00:01:20",
        "end": "00:01:55",
        "score": 95,
        "reason": "hook",
        "hashtags": ["#shorts", "motivation", "hindi ", "", "a", "b", "c", "d"],
        "description": "  Shorts description here.  ",
    }])

    seen = {}

    class FakeModels:
        def generate_content(self, model, contents):
            seen["prompt"] = contents
            class Resp:
                text = payload
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(gemini, "get_genai_client", lambda: FakeClient())
    clips = gemini.find_viral_clips("नमस्ते transcript", language="hi")
    assert len(clips) == 1
    c = clips[0]
    assert c["title"] == "सफलता का एक मंत्र"
    # '#' stripped, empties dropped, capped at 6.
    assert c["hashtags"] == ["shorts", "motivation", "hindi", "a", "b", "c"]
    assert c["description"] == "Shorts description here."
    assert "hi" in seen["prompt"]


def test_find_viral_clips_unparseable_raises(monkeypatch):
    import app.services.video.gemini as gemini
    import pytest

    class FakeModels:
        def generate_content(self, model, contents):
            class Resp:
                text = "not json at all {{{"
            return Resp()

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(gemini, "get_genai_client", lambda: FakeClient())
    with pytest.raises(RuntimeError):
        gemini.find_viral_clips("dummy")
