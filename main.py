import os
import sys
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai

load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")

DOWNLOAD_DIR = Path("downloads")
CLIPS_DIR = Path("clips")
TMP_DIR = Path("tmp")

for d in (DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR):
    d.mkdir(exist_ok=True)

client = genai.Client(api_key=GEMINI_API_KEY)

_face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


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


def detect_face_center_x(input_path: Path, at_seconds: float, frame_width: int) -> float:
    frame_path = TMP_DIR / f"frame-{os.getpid()}-{int(at_seconds * 1000)}.jpg"

    run([
        "ffmpeg", "-y",
        "-ss", str(at_seconds),
        "-i", str(input_path),
        "-frames:v", "1",
        str(frame_path),
    ])

    try:
        img = cv2.imread(str(frame_path))
        if img is None:
            return frame_width / 2

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = _face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )

        if len(faces) == 0:
            return frame_width / 2

        # pick the largest detected face as the main subject
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        face_center_x = x + w / 2
        img_width = img.shape[1]
        return (face_center_x / img_width) * frame_width
    finally:
        if frame_path.exists():
            frame_path.unlink()


def cut_clip(input_path: Path, output_path: Path, start: str, end: str) -> None:
    width, height = get_video_dimensions(input_path)

    start_sec = time_to_seconds(start)
    end_sec = time_to_seconds(end)
    mid_sec = start_sec + (end_sec - start_sec) / 2

    face_center_x = detect_face_center_x(input_path, mid_sec, width)

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