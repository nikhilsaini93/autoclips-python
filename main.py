import os
import sys
import json
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
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


def get_transcript(video_id: str) -> str:
    ytt_api = YouTubeTranscriptApi()
    fetched_transcript = ytt_api.fetch(video_id)
    lines = []
    for snippet in fetched_transcript:
        seconds = int(snippet.start)
        lines.append(f"[{seconds}] {snippet.text}")
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


def cut_clip(input_path: Path, output_path: Path, start: str, end: str) -> None:
    width, height = get_video_dimensions(input_path)

    start_sec = time_to_seconds(start)
    end_sec = time_to_seconds(end)

    face_center_x = detect_face_center_x(input_path, start_sec, end_sec, width)

    crop_width = round(height * 9 / 16)
    x = round(face_center_x - crop_width / 2)
    x = max(0, min(x, width - crop_width))

    run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ss", start,
        "-to", end,
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-vf", f"crop={crop_width}:{height}:{x}:0,scale=1080:1920,setsar=1",
        "-c:v", "libx264",
        "-c:a", "aac",
        str(output_path),
    ])


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python main.py <youtube-url>")

    youtube_url = sys.argv[1]
    video_id = get_video_id(youtube_url)

    print("Getting transcript...")
    transcript = get_transcript(video_id)

    print("Finding viral clips...")
    clips = find_viral_clips(transcript)
    print(clips)

    video_path = DOWNLOAD_DIR / f"{video_id}.mp4"

    print("Downloading video...")
    download_video(video_id, video_path)
    print("Video downloaded.")

    for i, clip in enumerate(clips, start=1):
        output_path = CLIPS_DIR / f"clip-{i}.mp4"
        print(f"Creating clip {i}")
        cut_clip(video_path, output_path, clip["start"], clip["end"])
        print(f"Saved: {output_path}")

    print("All clips generated.")


if __name__ == "__main__":
    main()