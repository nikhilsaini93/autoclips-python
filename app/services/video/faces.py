import logging
import os
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import (
    ASSETS_FACE_DIR,
    CAFFEMODEL_PATH,
    PROTOTXT_PATH,
    YOLO_PATH,
    YUNET_PATH,
    settings,
)
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
# Cached YOLO failure: try download/load once per process, then stay on YuNet
# instead of retrying (and spamming logs) for every sampled frame.
_yolo_unavailable = False
_yolo_warned = False
_yunet_warned = False
# Last detector actually used (yolo|yunet|res10); updated per frame so the
# clip-level summary log reports what ran, not just what was requested.
_last_actual_detector: str | None = None


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


def _yolo_weights_path() -> Path:
    """Resolve yolov8n-face.pt location.

    Priority: FACE_YOLO_WEIGHTS env (e.g. Drive-persisted file on Colab) then
    assets/face_detector/yolov8n-face.pt.
    """
    custom = (getattr(settings, "FACE_YOLO_WEIGHTS", "") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return YOLO_PATH


def ensure_yolo_model() -> Path:
    """Ensure yolov8n-face.pt exists locally, downloading once if needed.

    Uses direct HuggingFace URLs instead of ultralytics auto-download so we
    bypass api.github.com (403 rate-limited on Colab shared IPs). Raises
    RuntimeError if all URLs fail so callers fall back to YuNet.
    """
    path = _yolo_weights_path()
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        (getattr(settings, "YOLO_FACE_URL", "") or "").strip(),
        (getattr(settings, "YOLO_FACE_URL_FALLBACK", "") or "").strip(),
    ]
    urls = [u for u in urls if u]
    last_err: Exception | None = None
    for url in urls:
        try:
            logger.info("Downloading YOLO face weights (~6MB, one-time) from %s...", url)
            tmp = path.with_suffix(path.suffix + ".tmp")
            urllib.request.urlretrieve(url, tmp)
            if tmp.stat().st_size == 0:
                raise RuntimeError(f"empty download from {url}")
            tmp.replace(path)
            return path
        except Exception as e:
            last_err = e
            logger.warning("YOLO weights download failed from %s (%s)", url, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    raise RuntimeError(f"Could not download yolov8n-face.pt ({last_err})")


def _reset_face_state() -> None:
    """Test helper: clear cached models and fallback flags."""
    global _face_net, _yunet_net, _yolo_model
    global _yolo_unavailable, _yolo_warned, _yunet_warned, _last_actual_detector
    _face_net = None
    _yunet_net = None
    _yolo_model = None
    _yolo_unavailable = False
    _yolo_warned = False
    _yunet_warned = False
    _last_actual_detector = None


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

    Downloads weights once via ensure_yolo_model() (direct HF URL). Any
    failure marks YOLO unavailable for the rest of the process so callers
    fall back to YuNet/Res10 instead of retrying per frame.
    Raises ImportError/RuntimeError on failure."""
    global _yolo_model, _yolo_unavailable
    if _yolo_unavailable:
        raise RuntimeError("YOLO face model unavailable (cached failure)")
    if _yolo_model is None:
        with _yolo_lock:
            if _yolo_model is None:
                if _yolo_unavailable:
                    raise RuntimeError("YOLO face model unavailable (cached failure)")
                try:
                    from ultralytics import YOLO

                    import torch

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    weights = str(ensure_yolo_model())
                    _yolo_model = YOLO(weights)
                    try:
                        _yolo_model.to(device)
                    except Exception:
                        pass
                    logger.info("YOLO face model loaded (%s, device=%s).", weights, device)
                except Exception:
                    _yolo_unavailable = True
                    raise
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


def detect_faces_in_frame_with_kind(img) -> tuple[list, str]:
    """Same as detect_faces_in_frame but also returns actual detector used.

    YOLO is tried at most once per process: after the first failure
    _yolo_unavailable is set and later frames go straight to YuNet with only
    a debug log (first failure logs one warning).
    """
    global _yolo_warned, _yunet_warned, _last_actual_detector, _yolo_unavailable
    kind = detector_kind()
    if kind == "yolo" and not _yolo_unavailable:
        try:
            faces = _yolo_faces(img)
            _last_actual_detector = "yolo"
            return faces, "yolo"
        except Exception as e:
            _yolo_unavailable = True
            if not _yolo_warned:
                _yolo_warned = True
                logger.warning("YOLO face failed (%s), falling back to YuNet.", e)
            else:
                logger.debug("YOLO face failed (%s), falling back to YuNet.", e)
    elif kind == "yolo" and _yolo_unavailable:
        logger.debug("Skipping YOLO (cached failure), using YuNet.")
    if kind in ("yolo", "yunet"):
        try:
            faces = _yunet_faces(img)
            _last_actual_detector = "yunet"
            return faces, "yunet"
        except Exception as e:
            if not _yunet_warned:
                _yunet_warned = True
                logger.warning("YuNet face failed (%s), falling back to Res10.", e)
            else:
                logger.debug("YuNet face failed (%s), falling back to Res10.", e)
    faces = _res10_faces(img)
    _last_actual_detector = "res10"
    return faces, "res10"


def detect_faces_in_frame(img):
    """All faces in an image as [(center_x_px, conf, width_px)]. Dispatches by
    FACE_MODEL with graceful fallback: yolo -> yunet -> res10."""
    faces, _ = detect_faces_in_frame_with_kind(img)
    return faces


def detect_face_center_x_in_frame_with_kind(frame_path: Path, frame_width: int) -> tuple[float | None, str]:
    import cv2

    img = cv2.imread(str(frame_path))
    if img is None:
        return None, _last_actual_detector or detector_kind()
    img_height, img_width = img.shape[:2]
    try:
        faces, actual = detect_faces_in_frame_with_kind(img)
    except Exception:
        logger.exception("Face detection failed for %s", frame_path)
        return None, _last_actual_detector or detector_kind()
    if not faces:
        return None, actual
    # Prefer large confident faces (talking head) over tiny background faces.
    best = max(faces, key=lambda f: (f[1], f[2]))
    return (best[0] / img_width) * frame_width, actual


def detect_face_center_x_in_frame(frame_path: Path, frame_width: int):
    center, _ = detect_face_center_x_in_frame_with_kind(frame_path, frame_width)
    return center


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


def _effective_requested_kind() -> str:
    """Requested detector corrected for cached YOLO failure."""
    kind = detector_kind()
    if kind == "yolo" and _yolo_unavailable:
        return "yunet"
    return kind


def _summary_detector(actuals: list[str]) -> str:
    """Most common actual detector, or effective requested kind if none."""
    if actuals:
        return max(set(actuals), key=actuals.count)
    return _effective_requested_kind()


def detect_face_center_x(input_path: Path, start_sec: float, end_sec: float, frame_width: int) -> float:
    global _last_actual_detector
    logger.info("Running face detection (%s) over [%.1f, %.1f] for vertical crop...",
                _effective_requested_kind(), start_sec, end_sec)
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
            # Use shared run() helper (mockable in tests, logs stderr).
            run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(input_path), "-frames:v", "1", str(frame_path)]
            )
            return frame_path
        except Exception as e:
            logger.warning("Frame grab failed at t=%.1f: %s", t, e)
            return None

    with ThreadPoolExecutor(max_workers=min(num_samples, 4)) as pool:
        grabbed = list(pool.map(_grab_frame, zip(sample_times, frame_paths)))

    detected_centers = []
    actuals: list[str] = []
    try:
        for frame_path in grabbed:
            if frame_path is None:
                continue
            try:
                # Reset per-frame so we only record the detector used for
                # this frame (mocked wrappers leave it None -> ignored).
                _last_actual_detector = None
                center_x = detect_face_center_x_in_frame(frame_path, frame_width)
                if _last_actual_detector is not None:
                    actuals.append(_last_actual_detector)
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
    actual_kind = _summary_detector(actuals)
    if not detected_centers:
        logger.info("No face detected in %d sampled frames (%s, %.2fs) - falling back to center crop",
                    num_samples, actual_kind, elapsed)
        return frame_width / 2

    smoothed = smooth_centers(detected_centers)
    logger.info(
        "Face detected in %d/%d sampled frames (%s) in %.2fs",
        len(detected_centers), num_samples, actual_kind, elapsed,
    )
    try:
        import numpy as np

        return float(np.median(smoothed))
    except ImportError:
        return float(sum(smoothed) / len(smoothed))
