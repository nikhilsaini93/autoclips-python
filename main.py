# import os
# import sys
# import json
# import argparse
# import subprocess
# import urllib.request
# from pathlib import Path
# from urllib.parse import urlparse, parse_qs

# import cv2
# import numpy as np
# from dotenv import load_dotenv
# from faster_whisper import WhisperModel
# from google import genai

# load_dotenv()

# GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")

# DOWNLOAD_DIR = Path("downloads")
# CLIPS_DIR = Path("clips")
# TMP_DIR = Path("tmp")
# MODELS_DIR = Path("models")

# for d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, MODELS_DIR):
#     d.mkdir(exist_ok=True)

# client = genai.Client(api_key=GEMINI_API_KEY)

# # --- Face detector setup (OpenCV DNN, much more reliable than Haar cascades) ---

# PROTOTXT_PATH = MODELS_DIR / "deploy.prototxt"
# CAFFEMODEL_PATH = MODELS_DIR / "res10_300x300_ssd_iter_140000.caffemodel"

# PROTOTXT_URL = (
#     "https://raw.githubusercontent.com/opencv/opencv/master/"
#     "samples/dnn/face_detector/deploy.prototxt"
# )
# CAFFEMODEL_URL = (
#     "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
#     "dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
# )


# def ensure_face_model() -> None:
#     if not PROTOTXT_PATH.exists():
#         print("Downloading face detector config...")
#         urllib.request.urlretrieve(PROTOTXT_URL, PROTOTXT_PATH)
#     if not CAFFEMODEL_PATH.exists():
#         print("Downloading face detector weights (~10MB, one-time)...")
#         urllib.request.urlretrieve(CAFFEMODEL_URL, CAFFEMODEL_PATH)


# ensure_face_model()
# _face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))


# def get_video_id(url: str) -> str:
#     parsed = urlparse(url)
#     if parsed.hostname == "youtu.be":
#         return parsed.path.lstrip("/")
#     qs = parse_qs(parsed.query)
#     if "v" in qs:
#         return qs["v"][0]
#     raise ValueError("Invalid YouTube URL")


# WHISPER_MODEL_SIZE = "base"
# _whisper_model = None


# def get_whisper_model() -> WhisperModel:
#     global _whisper_model
#     if _whisper_model is None:
#         print(f"Loading Whisper model ({WHISPER_MODEL_SIZE})...")
#         _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
#     return _whisper_model


# def transcribe_video(video_id: str, video_path: Path) -> list:
#     """Transcribes the video's audio with Whisper, returning a list of
#     {start, end, text} per word. Cached to disk per video_id since
#     transcription is the slowest step and clips share one source video."""
#     cache_path = TMP_DIR / f"{video_id}-words.json"
#     if cache_path.exists():
#         return json.loads(cache_path.read_text())

#     model = get_whisper_model()
#     print("Transcribing audio with Whisper...")
#     segments, _info = model.transcribe(str(video_path), word_timestamps=True)

#     words = []
#     for segment in segments:
#         for word in segment.words:
#             words.append({
#                 "start": word.start,
#                 "end": word.end,
#                 "text": word.word.strip(),
#             })

#     cache_path.write_text(json.dumps(words))
#     return words


# def words_to_transcript_text(words: list) -> str:
#     """Groups words into ~12-word lines with a leading timestamp, in the
#     same '[seconds] text...' shape the clip-finding prompt expects."""
#     lines = []
#     buffer = []
#     line_start = None

#     for w in words:
#         if line_start is None:
#             line_start = w["start"]
#         buffer.append(w["text"])
#         if len(buffer) >= 12:
#             lines.append(f"[{int(line_start)}] {' '.join(buffer)}")
#             buffer = []
#             line_start = None

#     if buffer:
#         lines.append(f"[{int(line_start)}] {' '.join(buffer)}")

#     return "\n".join(lines)


# def find_viral_clips(transcript: str) -> list:
#     prompt = f"""
# You are a professional YouTube Shorts editor.

# Analyze the transcript and identify the TOP 5 clips.

# Rules:
# - Duration between 20 and 60 seconds.
# - Strong hook.
# - Valuable insight.
# - High engagement potential.
# - Understandable without full context.

# Return ONLY JSON.

# Format:

# [
#  {{
#    "title":"Clip Title",
#    "start":"00:01:20",
#    "end":"00:01:55",
#    "score":95,
#    "reason":"Curiosity hook"
#  }}
# ]

# Transcript:

# {transcript}
# """
#     response = client.models.generate_content(
#         model="gemini-3.5-flash",
#         contents=prompt,
#     )
#     text = (response.text or "").replace("```json", "").replace("```", "").strip()
#     return json.loads(text)


# def run(command: list) -> None:
#     subprocess.run(command, check=True)


# def download_video(video_id: str, output_path: Path) -> None:
#     url = f"https://www.youtube.com/watch?v={video_id}"
#     command = ["yt-dlp"]
#     if COOKIES_FILE:
#         command += ["--cookies", COOKIES_FILE]
#     command += [
#         # prefer H.264 (avc1) over AV1/VP9 - avc1 decodes far faster on CPU,
#         # which matters since every clip gets re-decoded multiple times
#         # (face-detection frame grabs + the final cut/subtitle burn)
#         "-f", "bv*[vcodec^=avc1]+ba/bv*+ba/b",
#         "--merge-output-format", "mp4",
#         "-o", str(output_path),
#         url,
#     ]
#     run(command)


# def get_video_dimensions(input_path: Path):
#     output = subprocess.run(
#         [
#             "ffprobe", "-v", "error",
#             "-select_streams", "v:0",
#             "-show_entries", "stream=width,height",
#             "-of", "csv=p=0",
#             str(input_path),
#         ],
#         capture_output=True, text=True, check=True,
#     ).stdout.strip()
#     width, height = map(int, output.split(","))
#     return width, height


# def time_to_seconds(time_str: str) -> float:
#     parts = [float(p) for p in time_str.split(":")]
#     if len(parts) == 3:
#         h, m, s = parts
#         return h * 3600 + m * 60 + s
#     m, s = parts
#     return m * 60 + s


# def detect_face_center_x_in_frame(frame_path: Path, frame_width: int) -> float | None:
#     """Returns the x-center (in original video pixel coords) of the largest
#     detected face in the given frame image, or None if no face was found."""
#     img = cv2.imread(str(frame_path))
#     if img is None:
#         return None

#     img_height, img_width = img.shape[:2]
#     blob = cv2.dnn.blobFromImage(
#         cv2.resize(img, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0)
#     )
#     _face_net.setInput(blob)
#     detections = _face_net.forward()

#     best_confidence = 0.0
#     best_center_x = None

#     for i in range(detections.shape[2]):
#         confidence = detections[0, 0, i, 2]
#         if confidence < 0.5:
#             continue
#         box = detections[0, 0, i, 3:7] * np.array(
#             [img_width, img_height, img_width, img_height]
#         )
#         start_x, _, end_x, _ = box
#         center_x = (start_x + end_x) / 2
#         if confidence > best_confidence:
#             best_confidence = confidence
#             best_center_x = center_x

#     if best_center_x is None:
#         return None

#     return (best_center_x / img_width) * frame_width


# def detect_face_center_x(
#     input_path: Path, start_sec: float, end_sec: float, frame_width: int
# ) -> float:
#     """Samples several frames across the clip's duration and returns the
#     median detected face x-center, falling back to the frame center if no
#     face is found in any sampled frame."""
#     num_samples = 5
#     sample_times = [
#         start_sec + (end_sec - start_sec) * (i + 1) / (num_samples + 1)
#         for i in range(num_samples)
#     ]

#     detected_centers = []

#     for t in sample_times:
#         frame_path = TMP_DIR / f"frame-{os.getpid()}-{int(t * 1000)}.jpg"
#         run([
#             "ffmpeg", "-y",
#             "-ss", str(t),
#             "-i", str(input_path),
#             "-frames:v", "1",
#             str(frame_path),
#         ])
#         try:
#             center_x = detect_face_center_x_in_frame(frame_path, frame_width)
#             if center_x is not None:
#                 detected_centers.append(center_x)
#         finally:
#             if frame_path.exists():
#                 frame_path.unlink()

#     if not detected_centers:
#         return frame_width / 2

#     return float(np.median(detected_centers))


# def format_srt_timestamp(seconds: float) -> str:
#     seconds = max(0.0, seconds)
#     hours = int(seconds // 3600)
#     minutes = int((seconds % 3600) // 60)
#     secs = int(seconds % 60)
#     millis = int(round((seconds - int(seconds)) * 1000))
#     return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


# def chunk_words_for_captions(words: list, max_words: int = 3):
#     """Groups consecutive words into short caption chunks, using each
#     chunk's actual first-word start and last-word end time — true
#     word-level sync instead of an estimate."""
#     chunks = []
#     for i in range(0, len(words), max_words):
#         group = words[i:i + max_words]
#         start = group[0]["start"]
#         end = group[-1]["end"]
#         text = " ".join(w["text"] for w in group)
#         chunks.append((start, end, text))
#     return chunks


# def build_clip_srt(words: list, start_sec: float, end_sec: float, srt_path: Path) -> bool:
#     """Writes an SRT file with timestamps relative to the clip's own start,
#     using only the words that fall inside [start_sec, end_sec], re-chunked
#     into short phrases. Returns True if any subtitle lines were written."""
#     clip_duration = end_sec - start_sec

#     clip_words = []
#     for w in words:
#         if w["end"] <= start_sec or w["start"] >= end_sec:
#             continue  # word doesn't overlap this clip at all
#         rel_start = max(0.0, w["start"] - start_sec)
#         rel_end = min(clip_duration, w["end"] - start_sec)
#         if rel_end <= rel_start:
#             continue
#         clip_words.append({"start": rel_start, "end": rel_end, "text": w["text"]})

#     if not clip_words:
#         return False

#     chunks = chunk_words_for_captions(clip_words)

#     with open(srt_path, "w", encoding="utf-8") as f:
#         for i, (rel_start, rel_end, text) in enumerate(chunks, start=1):
#             f.write(f"{i}\n")
#             f.write(
#                 f"{format_srt_timestamp(rel_start)} --> {format_srt_timestamp(rel_end)}\n"
#             )
#             f.write(f"{text}\n\n")

#     return True


# def cut_clip(
#     input_path: Path,
#     output_path: Path,
#     start: str,
#     end: str,
#     words: list | None = None,
# ) -> None:
#     width, height = get_video_dimensions(input_path)

#     start_sec = time_to_seconds(start)
#     end_sec = time_to_seconds(end)

#     face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)

#     crop_width = round(height * 9 / 16)
#     x = round(face_center_x - crop_width / 2)
#     x = max(0, min(x, width - crop_width))

#     vf_parts = [
#         "setpts=PTS-STARTPTS",
#         f"crop={crop_width}:{height}:{x}:0",
#         "scale=1080:1920",
#         "setsar=1",
#     ]

#     if words:
#         safe_name = f"clip-{start}-{end}.srt".replace(":", "-")
#         srt_path = TMP_DIR / safe_name
#         has_subs = build_clip_srt(words, start_sec, end_sec, srt_path)
#         if has_subs:
#             # ffmpeg's subtitles filter needs ':' escaped inside the path arg
#             escaped_path = str(srt_path).replace("\\", "/").replace(":", "\\:")
#             style = (
#                 "FontName=DejaVu Sans,FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,"
#                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
#                 "Alignment=2,MarginV=60"
#             )
#             vf_parts.append(f"subtitles='{escaped_path}':force_style='{style}'")

#     vf = ",".join(vf_parts)
#     duration_sec = end_sec - start_sec

#     run([
#         "ffmpeg", "-y",
#         "-ss", start,
#         "-i", str(input_path),
#         "-t", f"{duration_sec:.3f}",
#         "-map", "0:v:0",
#         "-map", "0:a:0",
#         "-vf", vf,
#         "-af", "asetpts=PTS-STARTPTS",
#         "-c:v", "libx264",
#         "-preset", "veryfast",
#         "-threads", "0",
#         "-c:a", "aac",
#         str(output_path),
#     ])


# def cmd_video_id(args) -> None:
#     print(json.dumps({"video_id": get_video_id(args.url)}))


# def cmd_download(args) -> None:
#     output_path = DOWNLOAD_DIR / f"{args.video_id}.mp4"
#     download_video(args.video_id, output_path)
#     print(json.dumps({"path": str(output_path)}))


# def cmd_transcript(args) -> None:
#     words = transcribe_video(args.video_id, Path(args.video_path))
#     print(json.dumps({"transcript": words_to_transcript_text(words)}))


# def cmd_clips(args) -> None:
#     transcript = sys.stdin.read()
#     clips = find_viral_clips(transcript)
#     print(json.dumps(clips))


# def cmd_cut(args) -> None:
#     # relies on transcribe_video's on-disk cache having already run for this video_id
#     words = transcribe_video(args.video_id, Path(args.input))
#     output_path = CLIPS_DIR / f"{args.video_id}-clip-{args.index}.mp4"
#     cut_clip(Path(args.input), output_path, args.start, args.end, words=words)
#     print(json.dumps({"path": str(output_path)}))


# def cmd_run(args) -> None:
#     video_id = get_video_id(args.url)
#     video_path = DOWNLOAD_DIR / f"{video_id}.mp4"

#     print("Downloading video...")
#     download_video(video_id, video_path)
#     print("Video downloaded.")

#     words = transcribe_video(video_id, video_path)
#     transcript_text = words_to_transcript_text(words)

#     print("Finding viral clips...")
#     clips = find_viral_clips(transcript_text)
#     print(clips)

#     for i, clip in enumerate(clips, start=1):
#         output_path = CLIPS_DIR / f"{video_id}-clip-{i}.mp4"
#         print(f"Creating clip {i}: {clip.get('title', '')}")
#         cut_clip(video_path, output_path, clip["start"], clip["end"], words=words)
#         print(f"Saved: {output_path}")

#     print("All clips generated.")


# def main() -> None:
#     parser = argparse.ArgumentParser()
#     sub = parser.add_subparsers(dest="command", required=True)

#     p = sub.add_parser("video-id")
#     p.add_argument("url")
#     p.set_defaults(func=cmd_video_id)

#     p = sub.add_parser("transcript")
#     p.add_argument("video_id")
#     p.add_argument("video_path")
#     p.set_defaults(func=cmd_transcript)

#     p = sub.add_parser("clips")  # reads transcript from stdin
#     p.set_defaults(func=cmd_clips)

#     p = sub.add_parser("download")
#     p.add_argument("video_id")
#     p.set_defaults(func=cmd_download)

#     p = sub.add_parser("cut")
#     p.add_argument("--input", required=True)
#     p.add_argument("--video-id", required=True, dest="video_id")
#     p.add_argument("--index", required=True)
#     p.add_argument("--start", required=True)
#     p.add_argument("--end", required=True)
#     p.set_defaults(func=cmd_cut)

#     p = sub.add_parser("run")
#     p.add_argument("url")
#     p.set_defaults(func=cmd_run)

#     args = parser.parse_args()
#     args.func(args)


# if __name__ == "__main__":
#     main()

import logging
import time
import uuid
from typing import List, Literal, Optional

from dotenv import load_dotenv

load_dotenv()

from logging_config import request_id_var, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from video_utils import (
    CLIPS_DIR,
    TMP_DIR,
    download_video,
    find_viral_clips,
    get_video_duration,
    get_video_id,
    render_video,
    time_to_seconds,
    transcribe_words_english,
    transcribe_words_native,
    words_to_transcript_text,
    write_srt,
    zip_clips,
)

app = FastAPI(
    title="YT Clip & Subtitle API",
    description="Download a YouTube video, get English subtitles, cut a clip by timestamp, or auto-find every viral-worthy clip.",
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Tags every log line produced while handling this request with a short
    request id (visible in console + logs/app.log), and logs start/end/timing."""
    req_id = uuid.uuid4().hex[:8]
    token = request_id_var.set(req_id)
    t0 = time.perf_counter()
    logger.info("--> %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("!! %s %s failed after %.1fs", request.method, request.url.path, time.perf_counter() - t0)
        request_id_var.reset(token)
        raise
    elapsed = time.perf_counter() - t0
    logger.info("<-- %s %s %d in %.1fs", request.method, request.url.path, response.status_code, elapsed)
    response.headers["X-Request-ID"] = req_id
    request_id_var.reset(token)
    return response


class SubtitleRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    burn_in: bool = Field(
        False, description="If true, returns an MP4 with subtitles burned in. If false, returns a .srt file."
    )
    vertical_crop: bool = Field(
        False, description="Only used when burn_in=true: crop the video to 9:16 vertical (face-aware)."
    )


class ClipRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    start: str = Field(..., description="Start time — 'HH:MM:SS', 'MM:SS', or seconds")
    end: str = Field(..., description="End time — 'HH:MM:SS', 'MM:SS', or seconds")
    vertical_crop: bool = Field(True, description="Crop to 9:16 vertical using face detection")
    subtitles: Literal["none", "english"] = Field(
        "none", description="Optionally burn English subtitles into the clip"
    )


class AnalyzeClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to let the AI decide the count entirely on its own."
    )


class ViralClipInfo(BaseModel):
    title: Optional[str] = None
    start: str
    end: str
    duration_seconds: float
    score: Optional[float] = None
    reason: Optional[str] = None


class ViralClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, description="Cap the number of clips. Omit to get every viral-worthy clip Gemini finds (typically 6-10)."
    )
    vertical_crop: bool = Field(True, description="Crop each clip to 9:16 vertical using face detection")
    subtitles: Literal["none", "english"] = Field(
        "english", description="Optionally burn English subtitles into each clip"
    )


def _prepare(url: str):
    try:
        video_id = get_video_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    video_path = download_video(video_id)
    return video_id, video_path


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/subtitles/english")
def subtitles_english(req: SubtitleRequest):
    """Transcribes the video and translates it to English (works even if the
    source audio is Hindi/Urdu/etc, via Whisper's translate task)."""
    logger.info("english subtitles requested url=%s burn_in=%s vertical_crop=%s", req.url, req.burn_in, req.vertical_crop)
    video_id, video_path = _prepare(req.url)
    words = transcribe_words_english(video_id, video_path)
    duration = get_video_duration(video_path)

    if not req.burn_in:
        srt_path = TMP_DIR / f"{video_id}-english.srt"
        if not write_srt(words, 0.0, duration, srt_path):
            raise HTTPException(422, "No speech detected")
        logger.info("Returning english srt for video_id=%s", video_id)
        return FileResponse(srt_path, filename=f"{video_id}-english.srt", media_type="application/x-subrip")

    output_path = CLIPS_DIR / f"{video_id}-english-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, 0.0, duration, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning english mp4 for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


@app.post("/clip")
def clip(req: ClipRequest):
    """Cuts [start, end] out of the video, optionally with a 9:16 face-aware
    crop and/or burned-in English subtitles."""
    logger.info(
        "clip requested url=%s start=%s end=%s vertical_crop=%s subtitles=%s",
        req.url, req.start, req.end, req.vertical_crop, req.subtitles,
    )
    video_id, video_path = _prepare(req.url)
    start_sec = time_to_seconds(req.start)
    end_sec = time_to_seconds(req.end)
    if end_sec <= start_sec:
        raise HTTPException(400, "end must be after start")

    words = transcribe_words_english(video_id, video_path) if req.subtitles == "english" else None

    output_path = CLIPS_DIR / f"{video_id}-clip-{uuid.uuid4().hex[:8]}.mp4"
    render_video(video_path, output_path, start_sec, end_sec, words=words, vertical_crop=req.vertical_crop)
    logger.info("Returning clip for video_id=%s -> %s", video_id, output_path.name)
    return FileResponse(output_path, filename=output_path.name, media_type="video/mp4")


def _analyze_viral_clips(video_id: str, video_path, max_clips: Optional[int]) -> list:
    """Shared by /clips/analyze and /clips/viral: builds a native-language
    transcript and asks Gemini to find every viral-worthy moment, with the
    AI itself deciding how many clips that is (unless max_clips caps it)."""
    native = transcribe_words_native(video_id, video_path)
    transcript_text = words_to_transcript_text(native["words"])
    try:
        candidates = find_viral_clips(transcript_text, max_clips=max_clips)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if not candidates:
        raise HTTPException(422, "Gemini did not find any viral-worthy clips in this video")
    return candidates


@app.post("/clips/analyze", response_model=List[ViralClipInfo])
def clips_analyze(req: AnalyzeClipsRequest):
    """AI-only analysis pass: no rendering, no video files produced. The
    model watches the transcript, decides for itself how many clips are
    worth cutting (anywhere from 1 to 10+), and returns each one's
    timestamps, a title, a confidence score, and its reasoning - so you can
    review the picks before spending time actually rendering any of them."""
    logger.info("clip analysis requested url=%s max_clips=%s", req.url, req.max_clips)
    video_id, video_path = _prepare(req.url)
    candidates = _analyze_viral_clips(video_id, video_path, req.max_clips)

    results = []
    for i, c in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(c["start"])
            end_sec = time_to_seconds(c["end"])
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed candidate #%d (%s): %s", i, c, e)
            continue
        if end_sec <= start_sec:
            logger.warning("Skipping candidate #%d with end<=start: %s", i, c)
            continue
        results.append(ViralClipInfo(
            title=c.get("title"),
            start=c["start"],
            end=c["end"],
            duration_seconds=round(end_sec - start_sec, 1),
            score=c.get("score"),
            reason=c.get("reason"),
        ))

    if not results:
        raise HTTPException(422, "All candidate clips were malformed")

    logger.info("Analysis found %d viral-worthy clip(s) for video_id=%s", len(results), video_id)
    return results


@app.post("/clips/viral")
def clips_viral(req: ViralClipsRequest):
    """Auto-finds every viral-worthy clip in the video (via Gemini) and
    renders ALL of them - not just the first one. Requires GEMINI_API_KEY.
    Returns a .zip containing each clip .mp4 plus a manifest.json with each
    clip's title/score/reason."""
    logger.info(
        "viral clips requested url=%s max_clips=%s vertical_crop=%s subtitles=%s",
        req.url, req.max_clips, req.vertical_crop, req.subtitles,
    )
    video_id, video_path = _prepare(req.url)
    candidates = _analyze_viral_clips(video_id, video_path, req.max_clips)
    english_words = transcribe_words_english(video_id, video_path) if req.subtitles == "english" else None

    entries = []
    for i, candidate in enumerate(candidates, start=1):
        try:
            start_sec = time_to_seconds(candidate["start"])
            end_sec = time_to_seconds(candidate["end"])
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed candidate #%d (%s): %s", i, candidate, e)
            continue
        if end_sec <= start_sec:
            logger.warning("Skipping candidate #%d with end<=start: %s", i, candidate)
            continue

        logger.info(
            "Rendering viral clip %d/%d: '%s' [%s -> %s] score=%s",
            i, len(candidates), candidate.get("title"), candidate.get("start"), candidate.get("end"), candidate.get("score"),
        )
        output_path = CLIPS_DIR / f"{video_id}-viral-{i}-{uuid.uuid4().hex[:8]}.mp4"
        render_video(video_path, output_path, start_sec, end_sec, words=english_words, vertical_crop=req.vertical_crop)
        entries.append({
            "path": output_path,
            "title": candidate.get("title"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "score": candidate.get("score"),
            "reason": candidate.get("reason"),
        })

    if not entries:
        raise HTTPException(422, "All candidate clips were malformed - nothing to render")

    zip_path = CLIPS_DIR / f"{video_id}-viral-{uuid.uuid4().hex[:8]}.zip"
    zip_clips(zip_path, entries)
    logger.info("Returning %d viral clip(s) for video_id=%s -> %s", len(entries), video_id, zip_path.name)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")