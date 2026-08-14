# YT Clip & Subtitle API

FastAPI server with three routes:

1. `POST /subtitles/english` — transcribe + translate a YouTube video's speech to English (`.srt`, or a burned-in-subtitles `.mp4`)
2. `POST /clip` — cut a clip from a YouTube video between `start` and `end`, with an optional 9:16 face-aware crop and optional burned-in English subtitles
3. `POST /clips/analyze` — AI-only analysis, no video rendering. The model decides on its own how many clips are worth cutting (not a fixed number), and returns each one's timestamps, title, confidence score, and reasoning as JSON.
4. `POST /clips/viral` — same AI analysis as above, but actually renders **every** clip it finds — not just the first one. Returns a `.zip` of every clip `.mp4` plus a `manifest.json` (title/score/reason per clip).

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

You also need `ffmpeg` and `ffprobe` on PATH (`sudo apt install ffmpeg` / `brew install ffmpeg`).

`GEMINI_API_KEY` in `.env` is only required for `/clips/analyze` and `/clips/viral` — the other two routes work without it.

## Run

```bash
uvicorn main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs

## Examples

**English subtitles (.srt file):**
```bash
curl -X POST http://localhost:8000/subtitles/english \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}' \
  -o english.srt
```

**Clip a video by timestamp, with English captions burned in:**
```bash
curl -X POST http://localhost:8000/clip \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "start": "00:01:20", "end": "00:01:55", "subtitles": "english"}' \
  -o clip.mp4
```

**Every viral clip Gemini finds (no cap — could be 1, could be 10+), vertical + English captions:**
```bash
curl -X POST http://localhost:8000/clips/viral \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}' \
  -o clips.zip
```
Unzip it — you'll get `manifest.json` plus one `.mp4` per clip. Pass `"max_clips": 5` in the body if you want to cap it instead of taking everything Gemini returns.

**Just the analysis — no rendering, get the picks back as JSON first:**
```bash
curl -X POST http://localhost:8000/clips/analyze \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```
```json
[
  {
    "title": "The one mistake everyone makes",
    "start": "00:04:12",
    "end": "00:04:48",
    "duration_seconds": 36.0,
    "score": 94,
    "reason": "Strong curiosity hook, self-contained insight"
  },
  { "title": "...", "start": "...", "end": "...", "duration_seconds": 41.0, "score": 88, "reason": "..." }
]
```
The count isn't fixed — the AI decides how many clips are actually worth cutting from this specific video, anywhere from 1 to 10+. Use this to review the picks, then call `/clip` for the ones you want, or just call `/clips/viral` to render all of them in one go.

## Logs

Every step logs progress with timing, since downloads/transcription/rendering can take a while:

- Console (stdout) — for watching `uvicorn` while it runs
- `logs/app.log` — rotating file (10 MB x 5 backups), so history survives restarts

Each request gets a short 8-char request id (also returned as the `X-Request-ID` response header), and every log line for that request — across `main.py` and `video_utils.py` — is tagged with it, so you can `grep req=abcd1234 logs/app.log` to follow one request end-to-end even with several running at once.

Example line:
```
2026-08-14 10:02:14 | INFO     | req=3f9a1c2b | video_utils | Downloading video_id=dQw4w9WgXcQ via yt-dlp...
2026-08-14 10:02:41 | INFO     | req=3f9a1c2b | video_utils | Download finished for video_id=dQw4w9WgXcQ in 27.3s (18.4 MB)
2026-08-14 10:03:05 | INFO     | req=3f9a1c2b | video_utils | Gemini returned 7 candidate clips in 4.2s
```

Set verbosity with `LOG_LEVEL` in `.env` (`DEBUG` also logs every ffmpeg/yt-dlp command it runs).

## Notes / design choices

- Every route downloads (and caches on disk under `downloads/`) the source video once per `video_id`, and Whisper transcripts are cached under `tmp/` per `(video_id, language, task)` — so calling multiple routes against the same video reuses work instead of redoing it.
- Routes are defined as regular (non-`async`) functions on purpose: FastAPI runs blocking `def` routes in a threadpool, which is what you want here since `yt-dlp`, Whisper, `ffmpeg`, and the Gemini call all block.
- Subtitles are English-only. `/clips/analyze` and `/clips/viral` use a *native-language* transcript (auto-detected, un-translated) to feed Gemini so clip boundaries line up with what's actually said, and a separate English-translated transcript only for `/clips/viral`'s burned-in captions.
- Neither route hard-caps the clip count by default — Gemini is asked for every clip that's genuinely viral-worthy and decides that count itself (pass `max_clips` if you want a ceiling). Malformed candidates (bad timestamps, end before start) are skipped with a warning rather than failing the whole batch.
- For long videos, requests can take a while (download + transcribe + ffmpeg encode all happen synchronously within the request; `/clips/viral` does this once per clip). If you need this to scale, the natural next step is to move processing into a background task/queue (e.g. Celery/RQ) and expose a job-status endpoint instead of blocking the HTTP response.