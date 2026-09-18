"""YOLO face-pipeline self-test: timestamp clip -> Telegram.

Verifies, in order (FAIL fast with a clear message):
  1. GPU visible (torch + CTranslate2 report only — YOLO works on CPU too).
  2. Ultralytics YOLO face model loads (downloads yolov8n-face.pt once).
  3. Source video resolves — local --video path, or cached YouTube download
     via download_video() (same cache as /clip, so re-runs reuse it).
  4. YOLO actually detects on a midpoint probe frame (reports face count).
  5. render_video() with vertical face-aware crop succeeds (proves the full
     crop path, not just the model import).
  6. Sends the clip straight to Telegram via Bot.send_video (no polling,
     no Approve buttons — pure delivery test).

Usage (local or Kaggle cell 10):
  python scripts/test_yolo_telegram.py --url <youtube-url> --start 00:01:20 --end 00:01:45
  python scripts/test_yolo_telegram.py --video /kaggle/working/my_video.mp4 --start 10 --end 30
  python scripts/test_yolo_telegram.py --url <url> --start 80 --end 100 --subtitles native --no-send

Secrets come from .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) like the app.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", default="", help="YouTube URL (downloaded + cached).")
    src.add_argument("--video", default="", help="Local video path (skips download).")
    p.add_argument("--start", required=True, help="Clip start: HH:MM:SS, MM:SS, or seconds.")
    p.add_argument("--end", required=True, help="Clip end: HH:MM:SS, MM:SS, or seconds.")
    p.add_argument("--subtitles", default="none", choices=["none", "english", "native", "hinglish"],
                   help="Burned-in subs. 'none' skips Whisper (fastest YOLO test).")
    p.add_argument("--face-model", default="yolo", choices=["yolo", "yunet", "res10"],
                   help="Detector under test (default yolo).")
    p.add_argument("--no-vertical", action="store_true",
                   help="Disable 9:16 crop (not recommended — crop is what YOLO is for).")
    p.add_argument("--no-send", action="store_true", help="Render only, skip Telegram send.")
    p.add_argument("--out", default="", help="Output filename (default: storage/clips/yolo-test-<ts>.mp4).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.perf_counter()

    from app.config import CLIPS_DIR, settings
    from app.services.video.timeutils import time_to_seconds

    try:
        start_sec = time_to_seconds(args.start)
        end_sec = time_to_seconds(args.end)
    except ValueError as e:
        print(f"FAIL bad timestamps: {e}")
        return 2
    if end_sec <= start_sec:
        print(f"FAIL end ({args.end}) must be after start ({args.start}).")
        return 2

    # -- Step 1: GPU report (info only) -------------------------------------
    try:
        import torch
        print(f"[1/6] torch {torch.__version__} cuda_available={torch.cuda.is_available()}", flush=True)
        if torch.cuda.is_available():
            print(f"       gpu: {torch.cuda.get_device_name(0)}", flush=True)
    except Exception as e:
        print(f"[1/6] torch unavailable: {e} (continuing — YOLO falls back to CPU)", flush=True)

    # -- Step 2: YOLO model loads --------------------------------------------
    settings.FACE_MODEL = args.face_model
    from app.services.video.faces import detector_kind, detect_faces_in_frame, get_yolo_model

    print(f"[2/6] FACE_MODEL={detector_kind()} (wants '{args.face_model}')", flush=True)
    if detector_kind() != "yolo":
        print("FAIL detector is not yolo — check FACE_MODEL / --face-model.")
        return 2
    try:
        import torch as _t
        model = get_yolo_model()
        device = getattr(getattr(model, "model", None), "device", None)
        cuda = _t.cuda.is_available()
        print(f"[2/6] PASS YOLO loaded (yolov8n-face.pt, torch_cuda={cuda}, model_device={device})", flush=True)
    except ImportError:
        print("FAIL ultralytics not installed — run: pip install -r requirements.txt")
        return 2
    except Exception as e:
        print(f"FAIL YOLO model would not load (falls back to YuNet/Res10 in prod): {type(e).__name__}: {e}")
        return 2

    # -- Step 3: resolve source video (cache reuse) ---------------------------
    if args.video:
        video_path = Path(args.video)
        if not video_path.exists():
            print(f"FAIL local video not found: {video_path}")
            return 2
        video_id = video_path.stem
        print(f"[3/6] using local video: {video_path}", flush=True)
    else:
        from app.services.video.download import download_video
        from app.services.video.ids import get_video_id
        try:
            video_id = get_video_id(args.url)
        except ValueError as e:
            print(f"FAIL bad URL: {e}")
            return 2
        print(f"[3/6] resolving video_id={video_id} (cached under storage/downloads/) ...", flush=True)
        try:
            video_path = download_video(video_id)
        except Exception as e:
            print(f"FAIL download failed (Kaggle IPs often need --video or cookies.txt): {type(e).__name__}: {e}")
            return 2
        print(f"[3/6] PASS source ready: {video_path}", flush=True)

    # -- Step 4: YOLO probe on a midpoint frame -------------------------------
    mid = (start_sec + end_sec) / 2.0
    probe = Path(tempfile.gettempdir()) / f"yolo-probe-{os.getpid()}.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(mid), "-i", str(video_path),
             "-frames:v", "1", "-q:v", "2", str(probe)],
            check=True, timeout=60,
        )
        import cv2
        img = cv2.imread(str(probe))
        if img is None:
            print("FAIL probe frame unreadable (ffmpeg produced no image).")
            return 2
        faces = detect_faces_in_frame(img)
        print(f"[4/6] YOLO probe @t={mid:.1f}s: {len(faces)} face(s) "
              f"{[(round(x), round(c, 2)) for x, c, _ in faces[:3]]}", flush=True)
        if not faces:
            print("WARN no face in probe frame — render continues with center-crop fallback; "
                  "pick a timestamp where a person is on screen for a stronger test.")
        else:
            print("[4/6] PASS YOLO detects faces", flush=True)
    except Exception as e:
        print(f"FAIL probe failed: {type(e).__name__}: {e}")
        return 2
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass

    # -- Step 5: render vertical clip (face-aware crop path) -------------------
    from app.services.video.render import render_video
    from app.services.viral import resolve_subtitle_words

    words = None
    if args.subtitles != "none":
        print(f"[5/6] transcribing ({args.subtitles}) for burn-in subs ...", flush=True)
        try:
            words = resolve_subtitle_words(video_id, video_path, args.subtitles)
            print(f"       subtitle words: {len(words) if words else 0}", flush=True)
        except Exception as e:
            print(f"WARN transcription failed, rendering without subs: {type(e).__name__}: {e}")
            words = None
    out = Path(args.out) if args.out else CLIPS_DIR / f"{video_id}-yolo-test-{int(time.time())}.mp4"
    print(f"[5/6] rendering [{args.start} -> {args.end}] vertical_crop={not args.no_vertical} ...", flush=True)
    try:
        render_video(video_path, out, start_sec, end_sec,
                     words=words, vertical_crop=not args.no_vertical)
    except Exception as e:
        print(f"FAIL render failed: {type(e).__name__}: {e}")
        return 2
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"[5/6] PASS rendered {out.name} ({size_mb:.1f} MB)", flush=True)

    # -- Step 6: send to Telegram ----------------------------------------------
    if args.no_send:
        print(f"[6/6] SKIP send (--no-send). File kept at: {out}")
    else:
        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID
        if not token or not chat_id:
            print("FAIL TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env — file kept at: "
                  f"{out}")
            return 2
        from telegram import Bot
        cap = float(getattr(settings, "TELEGRAM_MAX_VIDEO_MB", 50.0) or 50.0)
        if size_mb > cap:
            print(f"FAIL {size_mb:.1f} MB > {cap:.0f} MB Telegram cap — file kept at: {out}")
            return 2
        caption = (f"🧪 YOLO test [{args.start} → {args.end}]\n"
                   f"faces in probe: {len(faces)} | {out.name}")
        print("[6/6] sending to Telegram ...", flush=True)

        async def _send():
            bot = Bot(token=token)
            with open(out, "rb") as f:
                await bot.send_video(chat_id=chat_id, video=f, caption=caption[:1024])

        try:
            asyncio.run(_send())
        except Exception as e:
            print(f"FAIL telegram send failed: {type(e).__name__}: {e}")
            return 2
        print("[6/6] PASS sent to Telegram", flush=True)

    print(f"ALL DONE in {time.perf_counter() - t0:.1f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
