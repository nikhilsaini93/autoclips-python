import os
import sys
import json
import argparse
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound
from google import genai

load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")

DOWNLOAD_DIR = Path("downloads")
CLIPS_DIR = Path("clips")
TMP_DIR = Path("tmp")
MODELS_DIR = Path("models")

for d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, MODELS_DIR):
    d.mkdir(exist_ok=True)

client = genai.Client(api_key=GEMINI_API_KEY)

# --- Face detector setup (OpenCV DNN, much more reliable than Haar cascades) ---

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


def ensure_face_model() -> None:
    if not PROTOTXT_PATH.exists():
        print("Downloading face detector config...")
        urllib.request.urlretrieve(PROTOTXT_URL, PROTOTXT_PATH)
    if not CAFFEMODEL_PATH.exists():
        print("Downloading face detector weights (~10MB, one-time)...")
        urllib.request.urlretrieve(CAFFEMODEL_URL, CAFFEMODEL_PATH)


ensure_face_model()
_face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))


def get_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.lstrip("/")
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    raise ValueError("Invalid YouTube URL")


def get_transcript_snippets(video_id: str) -> list:
    ytt_api = YouTubeTranscriptApi()
    try:
        fetched_transcript = ytt_api.fetch(video_id, languages=("en",))
    except NoTranscriptFound:
        transcript_list = ytt_api.list(video_id)
        transcript = next(iter(transcript_list), None)
        if transcript is None:
            raise
        print(
            f"English transcript not found; using {transcript.language} "
            f"({transcript.language_code}).",
            file=sys.stderr,
        )
        fetched_transcript = transcript.fetch()

    return [
        {"start": s.start, "duration": s.duration, "text": s.text}
        for s in fetched_transcript
    ]


def get_transcript(video_id: str) -> str:
    snippets = get_transcript_snippets(video_id)
    lines = [f"[{int(s['start'])}] {s['text']}" for s in snippets]
    return "\n".join(lines)


def find_viral_clips(transcript: str) -> list:
    prompt = f"""
You are a professional YouTube Shorts editor.

Analyze the transcript and identify the TOP 5 clips.

Rules:
- Duration between 20 and 60 seconds.
- Strong hook.
- Valuable insight.
- High engagement potential.
- Understandable without full context.

Return ONLY JSON.

Format:

[
 {{
   "title":"Clip Title",
   "start":"00:01:20",
   "end":"00:01:55",
   "score":95,
   "reason":"Curiosity hook"
 }}
]

Transcript:

{transcript}
"""
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
    )
    text = (response.text or "").replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def run(command: list) -> None:
    subprocess.run(command, check=True)


def download_video(video_id: str, output_path: Path) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    command = ["yt-dlp"]
    if COOKIES_FILE:
        command += ["--cookies", COOKIES_FILE]
    command += [
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        url,
    ]
    run(command)


def get_video_dimensions(input_path: Path):
    output = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(input_path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = map(int, output.split(","))
    return width, height


def time_to_seconds(time_str: str) -> float:
    parts = [float(p) for p in time_str.split(":")]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    m, s = parts
    return m * 60 + s


def detect_face_center_x_in_frame(frame_path: Path, frame_width: int) -> float | None:
    """Returns the x-center (in original video pixel coords) of the largest
    detected face in the given frame image, or None if no face was found."""
    img = cv2.imread(str(frame_path))
    if img is None:
        return None

    img_height, img_width = img.shape[:2]
    blob = cv2.dnn.blobFromImage(
        cv2.resize(img, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0)
    )
    _face_net.setInput(blob)
    detections = _face_net.forward()

    best_confidence = 0.0
    best_center_x = None

    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence < 0.5:
            continue
        box = detections[0, 0, i, 3:7] * np.array(
            [img_width, img_height, img_width, img_height]
        )
        start_x, _, end_x, _ = box
        center_x = (start_x + end_x) / 2
        if confidence > best_confidence:
            best_confidence = confidence
            best_center_x = center_x

    if best_center_x is None:
        return None

    return (best_center_x / img_width) * frame_width


def detect_face_center_x(
    input_path: Path, start_sec: float, end_sec: float, frame_width: int
) -> float:
    """Samples several frames across the clip's duration and returns the
    median detected face x-center, falling back to the frame center if no
    face is found in any sampled frame."""
    num_samples = 5
    sample_times = [
        start_sec + (end_sec - start_sec) * (i + 1) / (num_samples + 1)
        for i in range(num_samples)
    ]

    detected_centers = []

    for t in sample_times:
        frame_path = TMP_DIR / f"frame-{os.getpid()}-{int(t * 1000)}.jpg"
        run([
            "ffmpeg", "-y",
            "-ss", str(t),
            "-i", str(input_path),
            "-frames:v", "1",
            str(frame_path),
        ])
        try:
            center_x = detect_face_center_x_in_frame(frame_path, frame_width)
            if center_x is not None:
                detected_centers.append(center_x)
        finally:
            if frame_path.exists():
                frame_path.unlink()

    if not detected_centers:
        return frame_width / 2

    return float(np.median(detected_centers))


def format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_clip_srt(video_id: str, start_sec: float, end_sec: float, srt_path: Path) -> bool:
    """Writes an SRT file with timestamps relative to the clip's own start,
    containing only the transcript lines that overlap [start_sec, end_sec].
    Returns True if any subtitle lines were written."""
    snippets = get_transcript_snippets(video_id)
    clip_duration = end_sec - start_sec

    entries = []
    for s in snippets:
        s_start = s["start"]
        s_end = s["start"] + s["duration"]

        if s_end <= start_sec or s_start >= end_sec:
            continue  # snippet doesn't overlap this clip at all

        rel_start = max(0.0, s_start - start_sec)
        rel_end = min(clip_duration, s_end - start_sec)
        if rel_end <= rel_start:
            continue

        entries.append((rel_start, rel_end, s["text"].replace("\n", " ")))

    if not entries:
        return False

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (rel_start, rel_end, text) in enumerate(entries, start=1):
            f.write(f"{i}\n")
            f.write(
                f"{format_srt_timestamp(rel_start)} --> {format_srt_timestamp(rel_end)}\n"
            )
            f.write(f"{text}\n\n")

    return True


def cut_clip(
    input_path: Path,
    output_path: Path,
    start: str,
    end: str,
    video_id: str | None = None,
) -> None:
    width, height = get_video_dimensions(input_path)

    start_sec = time_to_seconds(start)
    end_sec = time_to_seconds(end)

    face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)

    crop_width = round(height * 9 / 16)
    x = round(face_center_x - crop_width / 2)
    x = max(0, min(x, width - crop_width))

    vf_parts = [f"crop={crop_width}:{height}:{x}:0", "scale=1080:1920", "setsar=1"]

    if video_id:
        safe_name = f"{video_id}-{start}-{end}.srt".replace(":", "-")
        srt_path = TMP_DIR / safe_name
        has_subs = build_clip_srt(video_id, start_sec, end_sec, srt_path)
        if has_subs:
            # ffmpeg's subtitles filter needs ':' escaped inside the path arg
            escaped_path = str(srt_path).replace("\\", "/").replace(":", "\\:")
            style = (
                "FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
                "Alignment=2,MarginV=90"
            )
            vf_parts.append(f"subtitles='{escaped_path}':force_style='{style}'")

    vf = ",".join(vf_parts)

    run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ss", start,
        "-to", end,
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "aac",
        str(output_path),
    ])


def cmd_video_id(args) -> None:
    print(json.dumps({"video_id": get_video_id(args.url)}))


def cmd_transcript(args) -> None:
    print(json.dumps({"transcript": get_transcript(args.video_id)}))


def cmd_clips(args) -> None:
    transcript = sys.stdin.read()
    clips = find_viral_clips(transcript)
    print(json.dumps(clips))


def cmd_download(args) -> None:
    output_path = DOWNLOAD_DIR / f"{args.video_id}.mp4"
    download_video(args.video_id, output_path)
    print(json.dumps({"path": str(output_path)}))


def cmd_cut(args) -> None:
    output_path = CLIPS_DIR / f"{args.video_id}-clip-{args.index}.mp4"
    cut_clip(Path(args.input), output_path, args.start, args.end, video_id=args.video_id)
    print(json.dumps({"path": str(output_path)}))


def cmd_auto_url(args) -> None:
    video_id = get_video_id(args.url)
    transcript = get_transcript(video_id)
    clips = find_viral_clips(transcript)

    input_path = DOWNLOAD_DIR / f"{video_id}.mp4"
    if not input_path.exists():
        download_video(video_id, input_path)

    results = []
    for index, clip in enumerate(clips, start=1):
        output_path = CLIPS_DIR / f"{video_id}-clip-{index}.mp4"
        cut_clip(
            input_path,
            output_path,
            clip["start"],
            clip["end"],
            video_id=video_id,
        )
        results.append({
            "index": index,
            "title": clip.get("title"),
            "start": clip["start"],
            "end": clip["end"],
            "path": str(output_path),
        })

    print(json.dumps({"video_id": video_id, "clips": results}, indent=2))


def main() -> None:
    commands = {"video-id", "transcript", "clips", "download", "cut"}
    if len(sys.argv) == 2 and sys.argv[1] not in commands and not sys.argv[1].startswith("-"):
        cmd_auto_url(argparse.Namespace(url=sys.argv[1]))
        return

    parser = argparse.ArgumentParser()
    parser.epilog = 'Shortcut: python main.py "https://www.youtube.com/watch?v=..."'
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("video-id")
    p.add_argument("url")
    p.set_defaults(func=cmd_video_id)

    p = sub.add_parser("transcript")
    p.add_argument("video_id")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("clips")  # reads transcript from stdin
    p.set_defaults(func=cmd_clips)

    p = sub.add_parser("download")
    p.add_argument("video_id")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("cut")
    p.add_argument("--input", required=True)
    p.add_argument("--video-id", required=True, dest="video_id")
    p.add_argument("--index", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_cut)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
