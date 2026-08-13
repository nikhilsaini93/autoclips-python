# YT Clip & Subtitle API

FastAPI server with three routes:

1. `POST /subtitles/english` — transcribe + translate a YouTube video's speech to English (`.srt`, or a burned-in-subtitles `.mp4`)
2. `POST /subtitles/hinglish` — transcribe Hindi speech and Romanize it into Hinglish-style text (`.srt`, or burned-in `.mp4`)
3. `POST /clip` — cut a clip from a YouTube video between `start` and `end`, with an optional 9:16 face-aware crop and optional burned-in subtitles

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

You also need `ffmpeg` and `ffprobe` on PATH (`sudo apt install ffmpeg` / `brew install ffmpeg`).

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

**Hinglish subtitles, burned into a vertical video:**
```bash
curl -X POST http://localhost:8000/subtitles/hinglish \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "burn_in": true, "vertical_crop": true}' \
  -o hinglish.mp4
```

**Clip a video by timestamp, with English captions burned in:**
```bash
curl -X POST http://localhost:8000/clip \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "start": "00:01:20", "end": "00:01:55", "subtitles": "english"}' \
  -o clip.mp4
```

## Notes / design choices

- Every route downloads (and caches on disk under `downloads/`) the source video once per `video_id`, and Whisper transcripts are cached under `tmp/` per `(video_id, language, task)` — so calling multiple routes against the same video reuses work instead of redoing it.
- Routes are defined as regular (non-`async`) functions on purpose: FastAPI runs blocking `def` routes in a threadpool, which is what you want here since `yt-dlp`, Whisper, and `ffmpeg` all block.
- Hinglish subtitles are produced by transcribing Hindi audio (Devanagari script) with Whisper, then Romanizing word-by-word with `indic-transliteration`. It's a solid approximation, not a dedicated Hinglish model — spelling won't always match how people casually type Hinglish (e.g. "kya" vs "kyaa").
- English subtitles use Whisper's `translate` task, which converts speech in any detected source language directly to English text (not just Hindi).
- For long videos, these requests can take a while (download + transcribe + ffmpeg encode all happen synchronously within the request). If you need this to scale, the natural next step is to move processing into a background task/queue (e.g. Celery/RQ) and expose a job-status endpoint instead of blocking the HTTP response.