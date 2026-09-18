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

    # Cap transcript so 2h videos don't blow context/latency/cost. Keep head
    # (hook context) + truncate with a marker — prompt tells model the input
    # may be truncated.
    _MAX_TRANSCRIPT_CHARS = 90000
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = (
            transcript[:_MAX_TRANSCRIPT_CHARS]
            + f"\n...[truncated {len(transcript) - _MAX_TRANSCRIPT_CHARS} chars for length]..."
        )

    prompt = f"""
You are a professional YouTube Shorts editor.

Analyze the transcript and find the best clips for standalone short-form videos.
{language_instruction}
Transcript format: each line is [startSec-endSec] one spoken sentence in SECONDS,
with [pause Xs] markers where the speaker goes quiet. Use these to place cuts.
(The transcript may be truncated for length — only pick clips fully inside it.)
Rules:
- {count_instruction}
- Duration between 20 and 60 seconds. If a moment is shorter, EXTEND to the
  nearest sentence edge to reach 20s; if longer, SPLIT or TRUNCATE to 60s.
  Never return <8s or >180s.
- Strong hook in the first 2 seconds (question, bold claim, or payoff tease).
  Skip intros/outros/sponsor reads/CTAs ("like/subscribe") — clips must be
  self-contained and understandable without full context.
- Valuable insight, high engagement potential, shareable.
- Clips must not overlap each other. Rank best first by viral score.
- START each clip at a sentence start or right after a [pause]; END at a
  sentence end or inside a [pause]. NEVER cut mid-word or mid-sentence —
  move the boundary to the nearest sentence/pause edge instead.
- Title: upload-ready YouTube Shorts title in HINGLISH (Roman script) ONLY, max ~60 characters, short, punchy, curiosity hook, no clickbait lies, no Devanagari, no pure-English.
- Hashtags: 3-5 relevant tags in Hinglish/Roman script, lowercase, WITHOUT the '#' prefix.
- Description: 2-3 engaging lines in HINGLISH (Roman script) ONLY — explain what the viewer will learn + why to watch. No Devanagari, no pure-English. Do NOT add credit/link/disclaimer (added automatically later).

Return ONLY JSON (no markdown, no commentary).

Format (start/end as SECONDS number, e.g. 80.5 — HH:MM:SS also accepted):

[
  {{
    "title":"Success Ka Asli Secret (Hinglish title)",
    "start":80.0,
    "end":115.0,
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
    for attempt in range(1, 4):
        try:
            # Prefer JSON mode when the SDK supports it; fall back otherwise
            # (unit fakes accept only (model, contents)).
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt,
                    config={"response_mime_type": "application/json", "temperature": 0.4},
                )
            except TypeError:
                response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            text = (getattr(response, "text", None) or "").strip()
            # Robust fence strip: leading whitespace, ```json [...] on one
            # line, trailing prose after the fence.
            import re as _re
            m = _re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
            if m:
                text = m.group(1).strip()
            else:
                text = text.replace("```json", "").replace("```", "").strip()
                # Trailing prose after JSON: cut to first [...] / {...} block.
                if not text.startswith(("[", "{")):
                    s, e = text.find("["), text.rfind("]")
                    if s != -1 and e != -1 and e > s:
                        text = text[s:e + 1]
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
                if c.get("start") is None or c.get("end") is None:
                    logger.warning("Dropping candidate missing start/end: %s", c)
                    continue
                try:
                    ss = time_to_seconds(c["start"])
                    se = time_to_seconds(c["end"])
                except (ValueError, TypeError) as e:
                    logger.warning("Dropping candidate with bad timestamps %s (%s)", c, e)
                    continue
                if not (se > ss):
                    logger.warning("Dropping candidate with end<=start: %s", c)
                    continue
                dur = se - ss
                if dur < 8 or dur > 180:
                    logger.warning("Dropping candidate with out-of-range duration %.1fs: %s", dur, c)
                    continue
                # Normalize score to 0-100 float.
                try:
                    sc = float(c.get("score", 50))
                except (TypeError, ValueError):
                    sc = 50.0
                c["score"] = max(0.0, min(100.0, sc))
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
                c["_start_sec"] = ss
                c["_end_sec"] = se
                valid.append(c)
            # Dedupe overlaps: best score wins. Sort desc then keep
            # non-overlapping spans.
            valid.sort(key=lambda x: x.get("score", 0), reverse=True)
            deduped: list = []
            for c in valid:
                ss, se = c["_start_sec"], c["_end_sec"]
                overlaps = False
                for k in deduped:
                    if not (se <= k["_start_sec"] or ss >= k["_end_sec"]):
                        overlaps = True
                        logger.warning("Dropping overlapping clip %s (kept %s)", c, k)
                        break
                if not overlaps:
                    deduped.append(c)
            for c in deduped:
                c.pop("_start_sec", None)
                c.pop("_end_sec", None)
            clips = deduped
            last_error = None
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Gemini parse attempt %d/3 failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(2 ** attempt)
        except Exception as e:
            # API/network errors: retry with backoff instead of bubbling
            # immediately and killing the whole batch.
            last_error = e
            logger.warning("Gemini API attempt %d/3 failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(2 ** attempt)
    if last_error is not None:
        raise RuntimeError(f"Gemini failed after 3 attempts: {last_error}")
    if max_clips:
        clips = clips[:max_clips]
    logger.info("Gemini returned %d candidate clips in %.1fs", len(clips), time.perf_counter() - t0)
    for c in clips:
        logger.info("  candidate: [%s -> %s] score=%s '%s'", c.get("start"), c.get("end"), c.get("score"), c.get("title"))
    return clips
