import json
import logging
import threading
import time

from app.config import settings
from app.services.video.timeutils import time_to_seconds

logger = logging.getLogger(__name__)

_genai_client = None
_genai_lock = threading.Lock()


def get_genai_client():
    global _genai_client
    if _genai_client is None:
        with _genai_lock:
            if _genai_client is None:
                api_key = settings.GEMINI_API_KEY or ""
                if not api_key:
                    raise RuntimeError("GEMINI_API_KEY is not set - required for the viral-clip finder route.")
                from google import genai
                _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def find_viral_clips(transcript: str, max_clips: int | None = None, language: str | None = None) -> list:
    if max_clips:
        count_instruction = f"Identify up to {max_clips} clips, ranked best first."
    else:
        count_instruction = (
            "Identify EVERY clip in the video that's genuinely viral-worthy - "
            "could be 1, could be 10+. Don't artificially limit the count, "
            "but don't pad with weak clips either. Rank them best first."
        )

    if language:
        language_instruction = (
            f"The transcript language is '{language}'. IMPORTANT: regardless of the transcript "
            "language (Hindi, English, or anything else), ALWAYS write the title, hashtags and "
            "description in HINGLISH ONLY — Hindi + English mix written in Roman (English) script. "
            "NEVER use Devanagari or any other non-Roman script. "
            "Example: English video → Hinglish title like 'Success Ka Asli Secret'. "
        )
    else:
        language_instruction = (
            "Detect the transcript language, but ALWAYS write the title, hashtags and description "
            "in HINGLISH ONLY — Hindi + English mix written in Roman (English) script. "
            "NEVER use Devanagari or any other non-Roman script. "
            "Example: English video → Hinglish title like 'Success Ka Asli Secret'. "
        )

    prompt = f"""
You are a professional YouTube Shorts editor.

Analyze the transcript and find the best clips for standalone short-form videos.
{language_instruction}
Rules:
- {count_instruction}
- Duration between 20 and 60 seconds.
- Strong hook.
- Valuable insight.
- High engagement potential.
- Understandable without full context.
- Clips must not overlap each other.
- Title: upload-ready YouTube Shorts title in HINGLISH (Roman script) ONLY, max ~60 characters, short, punchy, curiosity hook, no clickbait lies, no Devanagari, no pure-English.
- Hashtags: 3-5 relevant tags in Hinglish/Roman script, lowercase, WITHOUT the '#' prefix.
- Description: 2-3 engaging lines in HINGLISH (Roman script) ONLY — explain what the viewer will learn + why to watch. No Devanagari, no pure-English. Do NOT add credit/link/disclaimer (added automatically later).

Return ONLY JSON.

Format:

[
  {{
    "title":"Success Ka Asli Secret (Hinglish title)",
    "start":"00:01:20",
    "end":"00:01:55",
    "score":95,
    "reason":"Curiosity hook",
    "hashtags":["shorts","motivation","successmindset"],
    "description":"Is clip me janiye success ka real funda jo har koi miss kar deta hai. End tak dekhna mat bhoolo."
  }}
]

Transcript:

{transcript}
"""
    logger.info(
        "Asking Gemini to find viral clips (transcript length=%d chars, language=%s)...",
        len(transcript), language or "auto",
    )
    t0 = time.perf_counter()
    client = get_genai_client()
    last_error: Exception | None = None
    clips: list = []
    for attempt in range(1, 3):
        try:
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            text = (response.text or "").strip()
            # Strip common markdown fences: ```json ... ``` or ``` ... ```
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.rstrip().endswith("```"):
                    text = text.rstrip()[:-3]
                text = text.replace("```json", "").replace("```", "").strip()
            else:
                text = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                # Tolerate {"clips": [...]} wrapper
                parsed = parsed.get("clips", [])
            if not isinstance(parsed, list):
                raise ValueError("Gemini response is not a JSON list")
            # Validate/shape each candidate, drop malformed ones early
            valid = []
            for c in parsed:
                if not isinstance(c, dict):
                    continue
                if not c.get("start") or not c.get("end"):
                    logger.warning("Dropping candidate missing start/end: %s", c)
                    continue
                try:
                    time_to_seconds(c["start"])
                    time_to_seconds(c["end"])
                except (ValueError, TypeError) as e:
                    logger.warning("Dropping candidate with bad timestamps %s (%s)", c, e)
                    continue
                # Normalize the YT upload pack (tolerate older Gemini replies
                # that only return title/start/end/score/reason).
                tags = c.get("hashtags", [])
                if isinstance(tags, str):
                    tags = [t.strip("# ").strip() for t in tags.replace(",", " ").split() if t.strip("# ").strip()]
                elif isinstance(tags, list):
                    tags = [str(t).strip("# ").strip() for t in tags if str(t).strip("# ").strip()]
                else:
                    tags = []
                c["hashtags"] = tags[:6]
                desc = c.get("description", "")
                c["description"] = str(desc).strip() if desc is not None else ""
                if not c.get("title"):
                    c["title"] = "Untitled Clip"
                valid.append(c)
            clips = valid
            last_error = None
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Gemini parse attempt %d/2 failed: %s", attempt, e)
    if last_error is not None:
        raise RuntimeError(f"Gemini returned unparseable JSON after 2 attempts: {last_error}")
    if max_clips:
        clips = clips[:max_clips]
    logger.info("Gemini returned %d candidate clips in %.1fs", len(clips), time.perf_counter() - t0)
    for c in clips:
        logger.info("  candidate: [%s -> %s] score=%s '%s'", c.get("start"), c.get("end"), c.get("score"), c.get("title"))
    return clips
