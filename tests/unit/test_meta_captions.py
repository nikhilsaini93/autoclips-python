import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


def test_clean_tags_dedupes_and_limits():
    from app.services.meta.captions import clean_tags

    tags = clean_tags(["#Shorts", "shorts ", "hindi news", "", "  ", "a", "b"], limit=4)
    assert tags == ["Shorts", "hindinews", "a", "b"]


def test_build_ig_caption_layout_and_footer():
    from app.services.meta.captions import build_ig_caption

    cap = build_ig_caption(
        "My Title", "My desc", ["shorts", "hindi"],
        video_id="abc123", credit="@chan",
    )
    assert cap.startswith("My Title")
    assert "My desc" in cap
    assert "Credit :- @chan" in cap
    assert "Original video link:- https://www.youtube.com/watch?v=abc123" in cap
    assert "#shorts" in cap and "#hindi" in cap
    assert len(cap) <= 2200


def test_build_ig_caption_truncates():
    from app.services.meta.captions import IG_CAPTION_LIMIT, build_ig_caption

    cap = build_ig_caption("T", "x" * 5000, ["a"])
    assert len(cap) <= IG_CAPTION_LIMIT


def test_build_ig_caption_caps_hashtags_at_30():
    from app.services.meta.captions import build_ig_caption

    cap = build_ig_caption("T", "D", [f"tag{i}" for i in range(50)])
    assert cap.count("#tag") == 30


def test_build_fb_description_and_title():
    from app.services.meta.captions import build_fb_description, build_fb_title

    assert build_fb_title("") == "Untitled Clip"
    assert len(build_fb_title("x" * 500)) <= 255
    desc = build_fb_description("Hi", ["a", "b"], video_id="v1", credit="@c")
    assert "Hi" in desc and "#a" in desc and "Credit :- @c" in desc
    assert "Original video link" in desc
