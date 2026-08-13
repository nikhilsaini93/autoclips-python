import os
import json
import time
import logging
import urllib.request
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from faster_whisper import WhisperModel
from indic_transliteration import sanscript
from indic_transliteration.sanscript import transliterate

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
        ensure_face_model()
        _face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))
        logger.debug("Face detector network loaded.")
    return _face_net


# ---------------------------------------------------------------------------
# Whisper (faster-whisper)
# ---------------------------------------------------------------------------
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
_whisper_model = None


def get_whisper_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        logger.info("Loading Whisper model '%s' (first call only, can take a while)...", WHISPER_MODEL_SIZE)
        t0 = time.perf_counter()
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        logger.info("Whisper model loaded in %.1fs", time.perf_counter() - t0)
    return _whisper_model


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------
def get_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.lstrip("/")
    else:
        qs = parse_qs(parsed.query)
        if "v" not in qs:
            logger.warning("Could not parse a video id out of URL: %s", url)
            raise ValueError("Invalid YouTube URL")
        video_id = qs["v"][0]
    logger.debug("Resolved video_id=%s from url=%s", video_id, url)
    return video_id


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
        # which matters since the clip gets re-decoded multiple times
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
def _transcribe_words(video_id: str, video_path: Path, language, task: str) -> list:
    """Runs Whisper and returns word-level timestamps. Cached to disk per
    (video_id, language, task) since transcription is the slowest step and
    every route/clip on the same source video can reuse it."""
    cache_key = f"{video_id}-{language or 'auto'}-{task}"
    cache_path = TMP_DIR / f"{cache_key}-words.json"
    if cache_path.exists():
        words = json.loads(cache_path.read_text())
        logger.info("Using cached transcript for %s (%d words)", cache_key, len(words))
        return words

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

    cache_path.write_text(json.dumps(words))
    logger.info(
        "Transcription done for %s: %d segments, %d words, detected_language=%s, in %.1fs",
        cache_key, segment_count, len(words), getattr(info, "language", "?"), time.perf_counter() - t0,
    )
    return words


def to_hinglish(text: str) -> str:
    """Best-effort Romanization of Devanagari text (Hindi script -> Hinglish-style Roman)."""
    try:
        return transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        logger.debug("Transliteration failed for word %r, leaving as-is", text, exc_info=True)
        return text


def transcribe_words_hinglish(video_id: str, video_path: Path) -> list:
    """Hindi speech, transcribed then transliterated word-by-word to Roman script."""
    words = _transcribe_words(video_id, video_path, language="hi", task="transcribe")
    logger.info("Transliterating %d words to Hinglish for video_id=%s...", len(words), video_id)
    t0 = time.perf_counter()
    result = [{"start": w["start"], "end": w["end"], "text": to_hinglish(w["text"])} for w in words]
    logger.info("Transliteration done in %.2fs", time.perf_counter() - t0)
    return result


def transcribe_words_english(video_id: str, video_path: Path) -> list:
    """Whisper's 'translate' task converts speech in any source language straight to English text."""
    return _transcribe_words(video_id, video_path, language=None, task="translate")


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
        if confidence < 0.5:
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
    num_samples = 5
    sample_times = [
        start_sec + (end_sec - start_sec) * (i + 1) / (num_samples + 1) for i in range(num_samples)
    ]
    detected_centers = []
    for t in sample_times:
        frame_path = TMP_DIR / f"frame-{os.getpid()}-{int(t * 1000)}.jpg"
        run(["ffmpeg", "-y", "-ss", str(t), "-i", str(input_path), "-frames:v", "1", str(frame_path)])
        try:
            center_x = detect_face_center_x_in_frame(frame_path, frame_width)
            if center_x is not None:
                detected_centers.append(center_x)
        finally:
            if frame_path.exists():
                frame_path.unlink()

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
    t0 = time.perf_counter()

    width, height = get_video_dimensions(input_path)
    vf_parts = ["setpts=PTS-STARTPTS"]

    if vertical_crop:
        face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)
        crop_width = round(height * 9 / 16)
        x = round(face_center_x - crop_width / 2)
        x = max(0, min(x, width - crop_width))
        vf_parts.append(f"crop={crop_width}:{height}:{x}:0")
        vf_parts.append("scale=1080:1920")
        vf_parts.append("setsar=1")

    if words:
        srt_path = TMP_DIR / f"sub-{os.getpid()}-{int(start_sec * 1000)}.srt"
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
        "-preset", "veryfast",
        "-threads", "0",
        "-c:a", "aac",
        str(output_path),
    ])

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Render complete: %s (%.1f MB) in %.1fs total",
        output_path, size_mb, time.perf_counter() - t0,
    )