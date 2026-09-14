# YT Clip & Subtitle API

FastAPI server with four routes + Telegram bot:

1. `POST /subtitles/english` — transcribe + translate a YouTube video's speech to English (`.srt`, or a burned-in-subtitles `.mp4`)
2. `POST /clip` — cut a clip from a YouTube video between `start` and `end`, with an optional 9:16 face-aware crop and burned-in subtitles (`none` | `english` | `native`)
3. `POST /clips/analyze` — AI-only analysis, no video rendering. The model decides on its own how many clips are worth cutting (not a fixed number), and returns each one's timestamps, upload-ready YT title (same language as the audio), hashtags, description, confidence score, and reasoning as JSON.
4. `POST /clips/viral` — same AI analysis as above, but actually renders **every** clip it finds — not just the first one. Sends each `.mp4` to Telegram and returns JSON metadata (`clip_id/file/title/hashtags/description/score/reason`).

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

You also need `ffmpeg` and `ffprobe` on PATH (`sudo apt install ffmpeg` / `brew install ffmpeg`).

`GEMINI_API_KEY` in `.env` is only required for `/clips/analyze` and `/clips/viral` — the other two routes work without it.

## Telegram Approve → YouTube upload

Every clip sent to Telegram carries **✅ Approve → YouTube (Unlisted)** and **❌ Deny (delete)** buttons.

- **Approve** uploads that clip as **Unlisted** with the AI title, description, and hashtags (as tags). Nothing is published — you schedule/publish later from YouTube Studio.
- **Deny** deletes just that clip MP4 and reports freed space. Source video + transcripts stay cached (use Delete-All / `POST /admin/cleanup` for full wipes).

One-time YouTube setup (~5 min, needed only for Approve):
1. Google Cloud project → enable **YouTube Data API v3**.
2. OAuth consent screen → External → add your Gmail as a test user.
3. Credentials → OAuth client ID → **Desktop app** → note client ID + secret (or download `client_secrets.json`).
4. `python scripts/get_youtube_token.py --secrets client_secrets.json` (or set `YT_CLIENT_ID`/`YT_CLIENT_SECRET` first) → sign in with the channel account → copy `YT_REFRESH_TOKEN` into `.env` → restart the bot.

Full click-by-click walkthrough with screenshots-described steps and troubleshooting: see **[YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md)**.

Quota note: one upload ≈ 1600 units; the default 10,000 units/day project quota ≈ **~6 uploads/day**. Failed uploads keep the clip file and offer a retry button.

## Run

```bash
uvicorn app.main:app --reload --port 8000
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

**Clip a video by timestamp, with native captions burned in:**
```bash
curl -X POST http://localhost:8000/clip \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "start": "00:01:20", "end": "00:01:55", "subtitles": "native"}' \
  -o clip.mp4
```
`subtitles` is `none` | `english` (translated) | `native` (as spoken, e.g. Hindi). Native reuses the same transcript as clip detection, so timing aligns exactly.

**Every viral clip Gemini finds (no cap — could be 1, could be 10+), vertical + native captions, sent to Telegram:**
```bash
curl -X POST http://localhost:8000/clips/viral \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "subtitles": "native"}'
```
Returns `{"success": true, "clips_sent": N, "clips": [...]}` and each clip is sent to the configured Telegram chat. Pass `"max_clips": 5` in the body if you want to cap it instead of taking everything Gemini returns.

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
    "reason": "Strong curiosity hook, self-contained insight",
    "hashtags": ["shorts", "motivation", "mindset"],
    "description": "The one mistake holding you back — explained in 36 seconds."
  },
  { "title": "...", "start": "...", "end": "...", "duration_seconds": 41.0, "score": 88, "reason": "...", "hashtags": [], "description": "..." }
]
```
Titles, hashtags and descriptions come back in the **same language as the audio** (Hindi video → Hindi titles), ready to paste into the YouTube Shorts upload.
The count isn't fixed — the AI decides how many clips are actually worth cutting from this specific video, anywhere from 1 to 10+. Use this to review the picks, then call `/clip` for the ones you want, or just call `/clips/viral` to render all of them in one go.

## Logs

Every step logs progress with timing, since downloads/transcription/rendering can take a while:

- Console (stdout) — for watching `uvicorn` while it runs
- `storage/logs/app.log` — rotating file (10 MB x 5 backups), so history survives restarts

Each request gets a short 8-char request id (also returned as the `X-Request-ID` response header), and every log line for that request — across `app/` services — is tagged with it, so you can `grep req=abcd1234 storage/logs/app.log` to follow one request end-to-end even with several running at once.

Example line:
```
2026-08-14 10:02:14 | INFO     | req=3f9a1c2b | video_utils | Downloading video_id=dQw4w9WgXcQ via yt-dlp...
2026-08-14 10:02:41 | INFO     | req=3f9a1c2b | video_utils | Download finished for video_id=dQw4w9WgXcQ in 27.3s (18.4 MB)
2026-08-14 10:03:05 | INFO     | req=3f9a1c2b | video_utils | Gemini returned 7 candidate clips in 4.2s
```

Set verbosity with `LOG_LEVEL` in `.env` (`DEBUG` also logs every ffmpeg/yt-dlp command it runs).

## Ops

- `GET /health` — ffmpeg/ffprobe availability, `GEMINI_API_KEY` set, disk free, Whisper model.
- `GET /jobs/{job_id}` — Telegram long generations (`ai_viral`, `timestamp`) report `running/done/failed` + progress here.
- `POST /admin/cleanup?max_age_hours=72` — purge `storage/downloads|clips|tmp|videos` files older than the TTL.
- Rate limits (per IP, no auth): `/subtitles/english` + `/clip` 20/min, `/clips/analyze` 10/min, `/clips/viral` 5/min.
- Telegram is private: only `TELEGRAM_CHAT_ID` + `TELEGRAM_ALLOWED_CHAT_IDS` are served, others get `⛔ Unauthorized`.
- Env knobs: `WHISPER_MODEL_SIZE/WHISPER_DEVICE/WHISPER_COMPUTE_TYPE`, `FFMPEG_PRESET`, `YOUTUBE_COOKIES_FILE`. See `.env.example`.

## Notes / design choices

- Every route downloads (and caches on disk under `storage/downloads/`) the source video once per `video_id`, and Whisper transcripts are cached under `storage/tmp/` per `(video_id, language, task)` — so calling multiple routes against the same video reuses work instead of redoing it.
- Routes are defined as regular (non-`async`) functions on purpose: FastAPI runs blocking `def` routes in a threadpool, which is what you want here since `yt-dlp`, Whisper, `ffmpeg`, and the Gemini call all block.
- Subtitles are `none` | `english` (Whisper translate) | `native` (as spoken). `/clips/analyze` and `/clips/viral` always analyze the *native-language* transcript (auto-detected) so clip boundaries line up with what's actually said; the burn-in track follows the `subtitles` you pick. Whisper defaults to the `small` model for better Hindi/Hinglish accuracy (override via `WHISPER_MODEL_SIZE`).
- Neither route hard-caps the clip count by default — Gemini is asked for every clip that's genuinely viral-worthy and decides that count itself (pass `max_clips` if you want a ceiling). Malformed candidates (bad timestamps, end before start) are skipped with a warning rather than failing the whole batch.
- For long videos, requests can take a while (download + transcribe + ffmpeg encode all happen synchronously within the request; `/clips/viral` does this once per clip). Telegram flows report progress via `GET /jobs/{job_id}`; the natural next step if this needs to scale further is a durable queue (e.g. Celery/RQ) instead of in-memory jobs.

## Structure

```
app/
  main.py              # FastAPI app + lifespan (uvicorn app.main:app)
  config.py            # Settings + storage/assets paths
  logging_config.py
  api/                 # schemas + routes (health/subtitles/clip/analyze/viral)
  telegram/            # bot, handlers, sending, progress, approvals
  services/
    video/             # ids/download/transcribe/gemini/subtitles/faces/render/cleanup
    youtube/           # descriptions/uploads
    jobs.py approvals.py viral.py
assets/face_detector/  # OpenCV DNN weights
scripts/               # get_youtube_token.py, send_telegram_test.py
docs/                  # YOUTUBE_SETUP.md
storage/               # gitignored: downloads/clips/tmp/videos/logs
tests/unit/
```
