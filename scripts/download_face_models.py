"""Pre-download face models so Colab/server never hits GitHub rate limits at runtime.

Downloads:
  - YuNet ONNX (~400KB) via app.services.video.faces.ensure_yunet_model
  - yolov8n-face.pt (~6MB) via ensure_yolo_model (direct HuggingFace URLs,
    bypassing api.github.com which 403s on Colab shared IPs)

Usage:
  python scripts/download_face_models.py
  python scripts/download_face_models.py --dir /content/drive/MyDrive/autoclips-config/models
  python scripts/download_face_models.py --yolo-only
  python scripts/download_face_models.py --check  # exit 0 if both present

When --dir is given, files are downloaded there and the script prints the
FACE_YOLO_WEIGHTS value to put in .env for Drive persistence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-download face detector weights.")
    parser.add_argument("--dir", default="", help="Custom dir for yolov8n-face.pt (e.g. Drive).")
    parser.add_argument("--yolo-only", action="store_true", help="Skip YuNet, download YOLO only.")
    parser.add_argument("--check", action="store_true", help="Only check presence, do not download.")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    import os

    if args.dir:
        os.environ["FACE_YOLO_WEIGHTS"] = str(Path(args.dir).expanduser() / "yolov8n-face.pt")

    # Import after env override so settings picks up FACE_YOLO_WEIGHTS.
    from app.config import YOLO_PATH, YUNET_PATH
    from app.services.video import faces

    if args.check:
        yolo_path = faces._yolo_weights_path()
        ok_yolo = yolo_path.exists() and yolo_path.stat().st_size > 0
        ok_yunet = YUNET_PATH.exists() and YUNET_PATH.stat().st_size > 0
        print(f"yolo: {yolo_path} {'OK' if ok_yolo else 'MISSING'}")
        if not args.yolo_only:
            print(f"yunet: {YUNET_PATH} {'OK' if ok_yunet else 'MISSING'}")
        return 0 if (ok_yolo and (args.yolo_only or ok_yunet)) else 1

    if not args.yolo_only:
        yunet = faces.ensure_yunet_model()
        print(f"yunet OK: {yunet} ({yunet.stat().st_size} bytes)")

    try:
        yolo = faces.ensure_yolo_model()
        print(f"yolo OK: {yolo} ({yolo.stat().st_size} bytes)")
    except Exception as e:
        print(f"yolo FAILED: {e}", file=sys.stderr)
        print("Hint: set FACE_MODEL=yunet in .env to run without YOLO.", file=sys.stderr)
        return 2

    if args.dir:
        print(f"\nAdd to .env:\nFACE_YOLO_WEIGHTS={faces._yolo_weights_path()}")
    else:
        print(f"\nDefault weights path (no .env change needed):\n{YOLO_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
