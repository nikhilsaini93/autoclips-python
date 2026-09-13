import os
import json
import time
import logging
import threading
import urllib.request
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("downloads")
CLIPS_DIR = Path("clips")
TMP_DIR = Path("tmp")
MODELS_DIR = Path("models")

for d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, MODELS_DIR):
    d.mkdir(exist_ok=True)

COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")

# ---------------------------------------------------------------------------
# Face detector (OpenCV DNN) - used for the 9:16 vertical crop
# ---------------------------------------------------------------------------
PROTOTXT_PATH = MODELS_DIR / "deploy.prototxt"
CAFFEMODEL_PATH = MODELS_DIR / "res10_300x300_ssd_iter_140000.caffemodel"
PROTOTXT_URL = (
    "https://raw.githubusercontent.com/opencv/opencv/master/"
    "samples/dnn/face_detector/deploy.prototxt"
)
CAFFEMODEL_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
    "dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
)

_face_net = None
_face_net_lock = threading.Lock()


def ensure_face_model() -> None:
    if not PROTOTXT_PATH.exists():
        logger.info("Downloading face detector config (deploy.prototxt)...")
        urllib.request.urlretrieve(PROTOTXT_URL, PROTOTXT_PATH)
    if not CAFFEMODEL_PATH.exists():
        logger.info("Downloading face detector weights (~10MB, one-time)...")
        urllib.request.urlretrieve(CAFFEMODEL_URL, CAFFEMODEL_PATH)
    logger.debug("Face detector model files ready.")


def get_face_net():
    global _face_net
    if _face_net is None:
        with _face_net_lock:
            if _face_net is None:
                ensure_face_model()
                _face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))
                logger.debug("Face detector network loaded.")
    return _face_net


# ---------------------------------------------------------------------------
# Whisper (faster-whisper)
# ---------------------------------------------------------------------------
# "base" is ~2x faster than "small" with acceptable quality for clip finding.
# Use "tiny" for maximum speed (lower accuracy). Set via WHISPER_MODEL_SIZE env var.
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
# "cpu" or "cuda". On CPU, "int8" is fastest; on CUDA, "float16" is best.
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                logger.info(
                    "Loading Whisper model '%s' (device=%s, compute=%s, first call only)...",
                    WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
                )
                t0 = time.perf_counter()
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE
                )
                logger.info("Whisper model loaded in %.1fs", time.perf_counter() - t0)
    return _whisper_model


# ---------------------------------------------------------------------------
# Gemini (used only by the viral-clip finder)
# ---------------------------------------------------------------------------
_genai_client = None
_genai_lock = threading.Lock()


def get_genai_client():
    global _genai_client
    if _genai_client is None:
        with _genai_lock:
            if _genai_client is None:
                api_key = os.environ.get("GEMINI_API_KEY")
                if not api_key:
                    raise RuntimeError("GEMINI_API_KEY is not set - required for the viral-clip finder route.")
                from google import genai
                _genai_client = genai.Client(api_key=api_key)
    return _genai_client


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------
_VIDEO_ID_RE = None


def _video_id_regex():
    global _VIDEO_ID_RE
    if _VIDEO_ID_RE is None:
        import re
        _VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
    return _VIDEO_ID_RE


def _validate_video_id(video_id: str, url: str) -> str:
    if not video_id or not _video_id_regex().match(video_id):
        logger.warning("Could not parse a video id out of URL: %s", url)
        raise ValueError("Invalid YouTube URL")
    return video_id


def get_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        # e.g. https://youtu.be/VIDEO_ID?t=10 — path holds the id
        video_id = parsed.path.lstrip("/").split("/")[0].split("?")[0].strip()
        return _validate_video_id(video_id, url)
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return _validate_video_id(qs["v"][0].strip(), url)
        # Support /shorts/ID, /embed/ID, /live/ID, /v/ID
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            return _validate_video_id(parts[1].split("?")[0].strip(), url)
    logger.warning("Could not parse a video id out of URL: %s", url)
    raise ValueError("Invalid YouTube URL")


def run(command: list) -> None:
    logger.debug("Running command: %s", " ".join(command))
    t0 = time.perf_counter()
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[-2000:]
        logger.error("Command failed (%s): %s\n%s", command[0], " ".join(command), stderr)
        raise
    logger.debug("Command finished in %.2fs: %s", time.perf_counter() - t0, command[0])


def download_video(video_id: str) -> Path:
    """Downloads (once) and caches the source video on disk, keyed by video_id."""
    output_path = DOWNLOAD_DIR / f"{video_id}.mp4"
    if output_path.exists():
        logger.info("Using cached download for video_id=%s (%s)", video_id, output_path)
        return output_path

    logger.info("Downloading video_id=%s via yt-dlp...", video_id)
    t0 = time.perf_counter()
    url = f"https://www.youtube.com/watch?v={video_id}"
    command = ["yt-dlp"]
    if COOKIES_FILE:
        command += ["--cookies", COOKIES_FILE]
    command += [
        # prefer H.264 (avc1) - decodes much faster on CPU than AV1/VP9,
        # which matters since clips get re-decoded multiple times
        # (face-detection frame grabs + the final cut/subtitle burn)
        "-f", "bv*[vcodec^=avc1]+ba/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        url,
    ]
    run(command)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Download finished for video_id=%s in %.1fs (%.1f MB)",
        video_id, time.perf_counter() - t0, size_mb,
    )
    return output_path


def get_video_credit(video_id: str) -> str:
    """Returns '@handle' credit for the source video (e.g. '@ranveerallahbadia').

    Uses yt-dlp metadata only (no download), cached under tmp/ so one video
    costs one lookup. Returns '' on any failure — callers must tolerate that.
    """
    try:
        cache_path = TMP_DIR / f"{video_id}.credit.txt"
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        url = f"https://www.youtube.com/watch?v={video_id}"
        command = ["yt-dlp", "--skip-download", "--no-warnings",
                   "--print", "%(uploader_id)s||%(channel)s||%(uploader)s||%(channel_url)s"]
        if COOKIES_FILE:
            command += ["--cookies", COOKIES_FILE]
        command.append(url)
        out = subprocess.run(command, capture_output=True, text=True, timeout=25)
        raw = (out.stdout or "").strip().splitlines()
        line = raw[-1].strip() if raw else ""
        parts = [p.strip() for p in line.split("||")]
        credit = ""
        for p in parts:
            if p and p.lower() != "na" and p.startswith("@"):
                credit = p
                break
        if not credit:
            # Fallback: build a handle from channel/uploader name.
            for p in parts:
                if p and p.lower() != "na":
                    # channel_url like https://www.youtube.com/@handle → keep handle
                    if "youtube.com/@" in p:
                        credit = "@" + p.split("/@")[-1].split("/")[0].strip()
                        break
            if not credit:
                for p in parts:
                    if p and p.lower() != "na":
                        credit = p if p.startswith("@") else f"@{p.replace(' ', '')}"
                        break
        if credit:
            try:
                cache_path.write_text(credit, encoding="utf-8")
            except OSError:
                pass
            return credit
    except Exception:
        logger.debug("Could not fetch credit for video_id=%s", video_id, exc_info=True)
    return ""


def get_video_dimensions(input_path: Path):
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = map(int, output.split(","))
    return width, height


def get_video_duration(input_path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(output)


def time_to_seconds(time_str) -> float:
    """Accepts 'HH:MM:SS', 'MM:SS', or a plain number of seconds."""
    if isinstance(time_str, (int, float)):
        return float(time_str)
    parts = [float(p) for p in str(time_str).split(":")]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return parts[0]


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def _transcribe_words(video_id: str, video_path: Path, language, task: str) -> dict:
    """Runs Whisper and returns {"language": <detected>, "words": [...]}.
    Cached to disk per (video_id, language, task) since transcription is the
    slowest step and every route/clip on the same source video reuses it."""
    cache_key = f"{video_id}-{language or 'auto'}-{task}"
    cache_path = TMP_DIR / f"{cache_key}-words.json"
    if cache_path.exists():
        try:
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(result.get("words"), list):
                logger.info("Using cached transcript for %s (%d words, language=%s)",
                            cache_key, len(result["words"]), result.get("language"))
                return result
            logger.warning("Transcript cache %s has bad shape, re-transcribing", cache_path)
        except (OSError, ValueError) as e:
            logger.warning("Ignoring corrupt transcript cache %s (%s), re-transcribing", cache_path, e)
        try:
            cache_path.unlink()
        except OSError:
            pass

    logger.info("Transcribing video_id=%s (language=%s, task=%s) with Whisper...", video_id, language or "auto", task)
    t0 = time.perf_counter()
    model = get_whisper_model()
    segments, info = model.transcribe(
        str(video_path), word_timestamps=True, language=language, task=task
    )

    words = []
    segment_count = 0
    for segment in segments:
        segment_count += 1
        for word in segment.words:
            words.append({"start": word.start, "end": word.end, "text": word.word.strip()})

    detected_language = getattr(info, "language", language or "unknown")
    result = {"language": detected_language, "words": words}
    tmp_cache = cache_path.with_suffix(".json.tmp")
    tmp_cache.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp_cache, cache_path)
    logger.info(
        "Transcription done for %s: %d segments, %d words, detected_language=%s, in %.1fs",
        cache_key, segment_count, len(words), detected_language, time.perf_counter() - t0,
    )
    return result


def transcribe_words_native(video_id: str, video_path: Path) -> dict:
    """Auto-detects the spoken language and transcribes in that language's
    native script (no translation). Used to feed the viral-clip finder."""
    return _transcribe_words(video_id, video_path, language=None, task="transcribe")


def transcribe_words_english(video_id: str, video_path: Path) -> list:
    """Whisper's 'translate' task converts speech in any source language straight to English text."""
    return _transcribe_words(video_id, video_path, language=None, task="translate")["words"]


def words_to_transcript_text(words: list) -> str:
    """Groups words into ~12-word lines with a leading timestamp, the
    '[seconds] text...' shape the viral-clip-finding prompt expects."""
    lines = []
    buffer = []
    line_start = None

    for w in words:
        if line_start is None:
            line_start = w["start"]
        buffer.append(w["text"])
        if len(buffer) >= 12:
            lines.append(f"[{int(line_start)}] {' '.join(buffer)}")
            buffer = []
            line_start = None

    if buffer:
        lines.append(f"[{int(line_start)}] {' '.join(buffer)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Viral clip finder (Gemini)
# ---------------------------------------------------------------------------
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
            response = client.models.generate_content(model="gemini-3.5-flash", contents=prompt)
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


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------
def format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def chunk_words_for_captions(words: list, max_words: int = 3):
    chunks = []
    for i in range(0, len(words), max_words):
        group = words[i:i + max_words]
        start = group[0]["start"]
        end = group[-1]["end"]
        text = " ".join(w["text"] for w in group)
        chunks.append((start, end, text))
    return chunks


def write_srt(words: list, start_sec: float, end_sec: float, srt_path: Path) -> bool:
    """Writes an SRT with timestamps relative to start_sec, using only the
    words that fall inside [start_sec, end_sec]. Returns False if empty."""
    clip_duration = end_sec - start_sec
    clip_words = []
    for w in words:
        if w["end"] <= start_sec or w["start"] >= end_sec:
            continue
        rel_start = max(0.0, w["start"] - start_sec)
        rel_end = min(clip_duration, w["end"] - start_sec)
        if rel_end <= rel_start:
            continue
        clip_words.append({"start": rel_start, "end": rel_end, "text": w["text"]})

    if not clip_words:
        logger.warning("No words fall inside [%.1f, %.1f] - not writing %s", start_sec, end_sec, srt_path)
        return False

    chunks = chunk_words_for_captions(clip_words)
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (rel_start, rel_end, text) in enumerate(chunks, start=1):
            f.write(f"{i}\n{format_srt_timestamp(rel_start)} --> {format_srt_timestamp(rel_end)}\n{text}\n\n")
    logger.info("Wrote %s (%d caption lines)", srt_path, len(chunks))
    return True


# ---------------------------------------------------------------------------
# Face-aware vertical crop
# ---------------------------------------------------------------------------
FACE_CONF_THRESHOLD = float(os.environ.get("FACE_CONF_THRESHOLD", "0.5"))

def detect_face_center_x_in_frame(frame_path: Path, frame_width: int):
    img = cv2.imread(str(frame_path))
    if img is None:
        return None
    img_height, img_width = img.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0))
    net = get_face_net()
    net.setInput(blob)
    detections = net.forward()

    best_confidence = 0.0
    best_center_x = None
    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence < FACE_CONF_THRESHOLD:
            continue
        box = detections[0, 0, i, 3:7] * np.array([img_width, img_height, img_width, img_height])
        start_x, _, end_x, _ = box
        center_x = (start_x + end_x) / 2
        if confidence > best_confidence:
            best_confidence = confidence
            best_center_x = center_x

    if best_center_x is None:
        return None
    return (best_center_x / img_width) * frame_width


def detect_face_center_x(input_path: Path, start_sec: float, end_sec: float, frame_width: int) -> float:
    logger.info("Running face detection over [%.1f, %.1f] for vertical crop...", start_sec, end_sec)
    t0 = time.perf_counter()
    # Reduced from 5 to 3 samples for speed; still covers clip well.
    num_samples = int(os.environ.get("FACE_DETECT_SAMPLES", "3"))
    sample_times = [
        start_sec + (end_sec - start_sec) * (i + 1) / (num_samples + 1) for i in range(num_samples)
    ]
    frame_tag = uuid.uuid4().hex[:8]
    frame_paths = [
        TMP_DIR / f"frame-{os.getpid()}-{frame_tag}-{int(t * 1000)}.jpg"
        for t in sample_times
    ]

    def _grab_frame(args) -> Path | None:
        t, frame_path = args
        try:
            # Some ffmpeg builds reject -vsync here; keeping the single-frame capture
            # without it is sufficient and works across Windows/Linux builds.
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(input_path), "-frames:v", "1", str(frame_path)])
            return frame_path
        except Exception:
            logger.exception("Frame grab failed at t=%.1f", t)
            return None

    # Parallelize ffmpeg frame grabs (subprocess/IO-bound); face detection
    # itself stays sequential because the shared OpenCV DNN Net is not
    # guaranteed thread-safe.
    with ThreadPoolExecutor(max_workers=min(num_samples, 3)) as pool:
        grabbed = list(pool.map(_grab_frame, zip(sample_times, frame_paths)))

    detected_centers = []
    try:
        for frame_path in grabbed:
            if frame_path is None:
                continue
            try:
                center_x = detect_face_center_x_in_frame(frame_path, frame_width)
                if center_x is not None:
                    detected_centers.append(center_x)
            except Exception:
                logger.exception("Face detection failed for %s", frame_path)
    finally:
        for frame_path in frame_paths:
            try:
                if frame_path.exists():
                    frame_path.unlink()
            except OSError:
                pass

    elapsed = time.perf_counter() - t0
    if not detected_centers:
        logger.info("No face detected in %d sampled frames (%.2fs) - falling back to center crop", num_samples, elapsed)
        return frame_width / 2

    logger.info(
        "Face detected in %d/%d sampled frames in %.2fs",
        len(detected_centers), num_samples, elapsed,
    )
    return float(np.median(detected_centers))


# ---------------------------------------------------------------------------
# Render: trim + optional 9:16 crop + optional burned-in subtitles
# ---------------------------------------------------------------------------
def render_video(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    words: list | None = None,
    vertical_crop: bool = True,
) -> None:
    logger.info(
        "Rendering %s -> %s [%.1f, %.1f] vertical_crop=%s subtitles=%s",
        input_path, output_path, start_sec, end_sec, vertical_crop, bool(words),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    width, height = get_video_dimensions(input_path)
    vf_parts = ["setpts=PTS-STARTPTS"]

    if vertical_crop:
        # Allow skipping face detection for speed (center crop fallback)
        if os.environ.get("SKIP_FACE_DETECT", "").lower() in ("1", "true", "yes"):
            logger.info("SKIP_FACE_DETECT=1 — using center crop")
            face_center_x = width / 2
        else:
            face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)
        crop_width = round(height * 9 / 16)
        x = round(face_center_x - crop_width / 2)
        x = max(0, min(x, width - crop_width))
        vf_parts.append(f"crop={crop_width}:{height}:{x}:0")
        vf_parts.append("scale=1080:1920")
        vf_parts.append("setsar=1")

    if words:
        srt_path = TMP_DIR / f"sub-{os.getpid()}-{uuid.uuid4().hex[:8]}.srt"
        has_subs = write_srt(words, start_sec, end_sec, srt_path)
        if has_subs:
            # ffmpeg's subtitles filter needs ':' escaped inside the path arg
            escaped_path = str(srt_path).replace("\\", "/").replace(":", "\\:")
            style = (
                "FontName=DejaVu Sans,FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
                "Alignment=2,MarginV=60"
            )
            vf_parts.append(f"subtitles='{escaped_path}':force_style='{style}'")

    vf = ",".join(vf_parts)
    duration_sec = end_sec - start_sec

    logger.info("Encoding with ffmpeg (duration=%.1fs)...", duration_sec)
    try:
        run([
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(input_path),
            "-t", f"{duration_sec:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-vf", vf,
            "-af", "asetpts=PTS-STARTPTS",
            "-c:v", "libx264",
            "-preset", os.environ.get("FFMPEG_PRESET", "veryfast"),
            "-threads", "0",
            "-c:a", "aac",
            str(output_path),
        ])
    finally:
        # Always clean up the per-render SRT so tmp/ doesn't grow forever.
        try:
            if words and srt_path.exists():
                srt_path.unlink()
        except OSError:
            pass

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Render complete: %s (%.1f MB) in %.1fs total",
        output_path, size_mb, time.perf_counter() - t0,
    )


def cleanup_old_files(max_age_hours: float = 72, max_total_mb: float | None = None) -> dict:
    """Delete files older than max_age_hours in downloads/clips/tmp.

    Optionally enforces a total size cap (oldest first) when max_total_mb is set.
    Returns {"deleted": N, "freed_mb": X}. Never deletes the folders themselves.
    """
    import shutil as _shutil

    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    freed = 0

    folders = [DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR]
    candidates: list[Path] = []
    for folder in folders:
        if not folder.exists():
            continue
        for item in folder.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    candidates.append(item)
                elif item.is_dir():
                    # Count nested dirs as one candidate by oldest mtime inside
                    try:
                        mtime = min(
                            (p.stat().st_mtime for p in item.rglob("*") if p.is_file()),
                            default=item.stat().st_mtime,
                        )
                    except OSError:
                        mtime = item.stat().st_mtime
                    candidates.append(item)
            except OSError:
                continue

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def _size(p: Path) -> int:
        try:
            if p.is_file():
                return p.stat().st_size
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return 0

    # 1) Age-based purge
    for item in list(candidates):
        try:
            if _mtime(item) < cutoff:
                freed += _size(item)
                if item.is_file() or item.is_symlink():
                    item.unlink()
                else:
                    _shutil.rmtree(item)
                deleted += 1
                candidates.remove(item)
        except OSError:
            logger.exception("Cleanup failed for %s", item)

    # 2) Optional total-size cap, oldest first
    if max_total_mb is not None:
        total_mb = sum(_size(p) for p in candidates) / (1024 * 1024)
        for item in sorted(candidates, key=_mtime):
            if total_mb <= max_total_mb:
                break
            try:
                size_mb = _size(item) / (1024 * 1024)
                if item.is_file() or item.is_symlink():
                    item.unlink()
                else:
                    _shutil.rmtree(item)
                deleted += 1
                freed += int(size_mb * 1024 * 1024)
                total_mb -= size_mb
            except OSError:
                logger.exception("Cleanup (size cap) failed for %s", item)

    logger.info("Cleanup: deleted %d item(s), freed %.1f MB", deleted, freed / (1024 * 1024))
    return {"deleted": deleted, "freed_mb": round(freed / (1024 * 1024), 1)}