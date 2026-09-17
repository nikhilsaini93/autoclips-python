import logging
import os
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import ASSETS_FACE_DIR, CAFFEMODEL_PATH, PROTOTXT_PATH, YUNET_PATH, settings
from app.services.video.process import run

logger = logging.getLogger(__name__)

PROTOTXT_URL = (
    "https://raw.githubusercontent.com/opencv/opencv/master/"
    "samples/dnn/face_detector/deploy.prototxt"
)
CAFFEMODEL_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
    "dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
)
# YuNet ONNX (~400KB) from opencv_zoo; no new pip dep, much better than Res10.
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)

_face_net = None
_face_net_lock = threading.Lock()
_yunet_net = None
_yunet_lock = threading.Lock()
_yolo_model = None
_yolo_lock = threading.Lock()


def ensure_face_model() -> None:
    ASSETS_FACE_DIR.mkdir(parents=True, exist_ok=True)
    if not PROTOTXT_PATH.exists():
        logger.info("Downloading face detector config (deploy.prototxt)...")
        urllib.request.urlretrieve(PROTOTXT_URL, PROTOTXT_PATH)
    if not CAFFEMODEL_PATH.exists():
        logger.info("Downloading face detector weights (~10MB, one-time)...")
        urllib.request.urlretrieve(CAFFEMODEL_URL, CAFFEMODEL_PATH)
    logger.debug("Face detector model files ready.")


def ensure_yunet_model() -> Path:
    ASSETS_FACE_DIR.mkdir(parents=True, exist_ok=True)
    if not YUNET_PATH.exists():
        logger.info("Downloading YuNet face weights (~400KB, one-time)...")
        urllib.request.urlretrieve(YUNET_URL, YUNET_PATH)
    return YUNET_PATH


def detector_kind() -> str:
    """Active detector: yolo > yunet > res10. Unknown values fall back to res10."""
    kind = (getattr(settings, "FACE_MODEL", "yolo") or "yolo").strip().lower()
    return kind if kind in ("yolo", "yunet", "res10") else "res10"


def get_face_net():
    global _face_net
    if _face_net is None:
        with _face_net_lock:
            if _face_net is None:
                import cv2

                ensure_face_model()
                _face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))
                logger.debug("Face detector network loaded (Res10).")
    return _face_net


def get_yunet_net():
    """cv2.FaceDetectorYN instance (input size set per frame)."""
    global _yunet_net
    if _yunet_net is None:
        with _yunet_lock:
            if _yunet_net is None:
                import cv2

                model_path = str(ensure_yunet_model())
                _yunet_net = cv2.FaceDetectorYN.create(model_path, "", (320, 320))
                logger.debug("YuNet face detector loaded.")
    return _yunet_net


def get_yolo_model():
    """Ultralytics YOLO face model on CUDA when available, else CPU.

    Raises ImportError/RuntimeError so callers can fall back to YuNet/Res10
    instead of crashing when ultralytics isn't installed (CPU boxes)."""
    global _yolo_model
    if _yolo_model is None:
        with _yolo_lock:
            if _yolo_model is None:
                from ultralytics import YOLO

                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
                _yolo_model = YOLO("yolov8n-face.pt")
                try:
                    _yolo_model.to(device)
                except Exception:
                    pass
                logger.info("YOLO face model loaded (yolov8n-face.pt, device=%s).", device)
    return _yolo_model


def _res10_faces(img):
    """Returns [(center_x_px, conf, box_w)] in image pixels via legacy Res10."""
    import cv2
    import numpy as np

    img_height, img_width = img.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0))
    net = get_face_net()
    net.setInput(blob)
    detections = net.forward()
    out = []
    threshold = settings.FACE_CONF_THRESHOLD
    for i in range(detections.shape[2]):
        confidence = float(detections[0, 0, i, 2])
        if confidence < threshold:
            continue
        box = detections[0, 0, i, 3:7] * np.array([img_width, img_height, img_width, img_height])
        start_x, _, end_x, _ = box
        out.append(((float(start_x) + float(end_x)) / 2.0, confidence, float(end_x - start_x)))
    return out


def _yunet_faces(img):
    import cv2

    img_height, img_width = img.shape[:2]
    net = get_yunet_net()
    try:
        net.setInputSize((img_width, img_height))
    except Exception:
        pass
    _, faces = net.detect(img)
    out = []
    threshold = settings.FACE_CONF_THRESHOLD
    if faces is None:
        return out
    for f in faces:
        # f: [x, y, w, h, ..., score]
        try:
            score = float(f[-1])
        except (TypeError, ValueError):
            continue
        if score < threshold:
            continue
        x, y, w, h = (float(v) for v in f[:4])
        out.append((x + w / 2.0, score, w))
    return out


def _yolo_faces(img):
    import numpy as np

    model = get_yolo_model()
    # verbose=False keeps Colab logs readable at 1fps sampling.
    results = model.predict(img, verbose=False)
    out = []
    threshold = settings.FACE_CONF_THRESHOLD
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        xyxy = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, "cpu") else np.asarray(boxes.xyxy)
        conf = boxes.conf.cpu().numpy() if hasattr(boxes.conf, "cpu") else np.asarray(boxes.conf)
        for (x1, y1, x2, y2), c in zip(xyxy, conf):
            if float(c) < threshold:
                continue
            out.append(((float(x1) + float(x2)) / 2.0, float(c), float(x2 - x1)))
    return out


def detect_faces_in_frame(img):
    """All faces in an image as [(center_x_px, conf, width_px)]. Dispatches by
    FACE_MODEL with graceful fallback: yolo -> yunet -> res10."""
    kind = detector_kind()
    if kind == "yolo":
        try:
            return _yolo_faces(img)
        except Exception as e:
            logger.warning("YOLO face failed (%s), falling back to YuNet.", e)
    if kind in ("yolo", "yunet"):
        try:
            return _yunet_faces(img)
        except Exception as e:
            logger.warning("YuNet face failed (%s), falling back to Res10.", e)
    return _res10_faces(img)


def detect_face_center_x_in_frame(frame_path: Path, frame_width: int):
    import cv2

    img = cv2.imread(str(frame_path))
    if img is None:
        return None
    img_height, img_width = img.shape[:2]
    try:
        faces = detect_faces_in_frame(img)
    except Exception:
        logger.exception("Face detection failed for %s", frame_path)
        return None
    if not faces:
        return None
    # Prefer large confident faces (talking head) over tiny background faces.
    best = max(faces, key=lambda f: (f[1], f[2]))
    return (best[0] / img_width) * frame_width


def _sample_times(start_sec: float, end_sec: float) -> list:
    duration = max(0.0, end_sec - start_sec)
    if duration <= 0:
        return []
    fps = float(getattr(settings, "FACE_SAMPLE_FPS", 0) or 0)
    if fps > 0:
        # Dense 1fps sampling on T4; clamp so a 10-min video doesn't spawn 600 grabs.
        n = max(3, min(60, int(round(duration * fps))))
    else:
        n = int(getattr(settings, "FACE_DETECT_SAMPLES", 0) or 3)
        n = max(1, min(60, n))
    # Even coverage excluding exact endpoints (hook at t=0 often has a cut fade).
    return [start_sec + duration * (i + 1) / (n + 1) for i in range(n)]


def _frame_dir() -> Path:
    """Ephemeral dir for sampled frames. Uses system /tmp instead of Drive-backed
    storage/tmp on Colab so 1fps grabs don't stall on Drive FUSE latency."""
    try:
        from pathlib import Path as _P

        sys_tmp = _P(tempfile.gettempdir())
        d = sys_tmp / "autoclips-frames"
        d.mkdir(parents=True, exist_ok=True)
        # Probe writability; fall back to storage/tmp if locked down.
        probe = d / ".writetest"
        try:
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
            return d
        except OSError:
            pass
    except Exception:
        pass
    from app.config import TMP_DIR

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR


def smooth_centers(centers: list, window: int | None = None) -> list:
    """Moving-average smoothing of crop centers to avoid jitter between samples."""
    if not centers:
        return []
    w = int(window or getattr(settings, "FACE_SMOOTH_WINDOW", 5) or 5)
    w = max(1, w)
    if w == 1 or len(centers) == 1:
        return list(centers)
    out = []
    for i in range(len(centers)):
        lo = max(0, i - w + 1)
        seg = centers[lo:i + 1]
        out.append(sum(seg) / len(seg))
    return out


def detect_face_center_x(input_path: Path, start_sec: float, end_sec: float, frame_width: int) -> float:
    logger.info("Running face detection (%s) over [%.1f, %.1f] for vertical crop...",
                detector_kind(), start_sec, end_sec)
    t0 = time.perf_counter()
    sample_times = _sample_times(start_sec, end_sec)
    num_samples = len(sample_times)
    if not sample_times:
        return frame_width / 2
    frame_tag = uuid.uuid4().hex[:8]
    fdir = _frame_dir()
    frame_paths = [
        fdir / f"frame-{os.getpid()}-{frame_tag}-{int(t * 1000)}.jpg"
        for t in sample_times
    ]

    def _grab_frame(args) -> Path | None:
        t, frame_path = args
        try:
            # Fast seek (-ss BEFORE -i): faster and avoids exit 255 errors 
            # if the timestamp is slightly past the end of the video.
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(input_path),
                 "-frames:v", "1", str(frame_path)])
            return frame_path
        except Exception as e:
            logger.warning("Frame grab failed at t=%.1f: %s", t, e)
            return None

    with ThreadPoolExecutor(max_workers=min(num_samples, 4)) as pool:
        grabbed = list(pool.map(_grab_frame, zip(sample_times, frame_paths)))

    detected_centers = []
    try:
        for frame_path in grabbed:
            if frame_path is None:
                continue
            try:
                center_x = detect_face_center_x_in_frame(frame_path, frame_width)
                if center_x is not None:
                    detected_centers.append(center_x)
            except Exception:
                logger.exception("Face detection failed for %s", frame_path)
    finally:
        for frame_path in frame_paths:
            try:
                if frame_path.exists():
                    frame_path.unlink()
            except OSError:
                pass

    elapsed = time.perf_counter() - t0
    if not detected_centers:
        logger.info("No face detected in %d sampled frames (%.2fs) - falling back to center crop", num_samples, elapsed)
        return frame_width / 2

    smoothed = smooth_centers(detected_centers)
    logger.info(
        "Face detected in %d/%d sampled frames (%s) in %.2fs",
        len(detected_centers), num_samples, detector_kind(), elapsed,
    )
    try:
        import numpy as np

        return float(np.median(smoothed))
    except ImportError:
        return float(sum(smoothed) / len(smoothed))
