import logging
import os
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import CAFFEMODEL_PATH, PROTOTXT_PATH, TMP_DIR, settings
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

_face_net = None
_face_net_lock = threading.Lock()


def ensure_face_model() -> None:
    if not PROTOTXT_PATH.exists():
        logger.info("Downloading face detector config (deploy.prototxt)...")
        urllib.request.urlretrieve(PROTOTXT_URL, PROTOTXT_PATH)
    if not CAFFEMODEL_PATH.exists():
        logger.info("Downloading face detector weights (~10MB, one-time)...")
        urllib.request.urlretrieve(CAFFEMODEL_URL, CAFFEMODEL_PATH)
    logger.debug("Face detector model files ready.")


def get_face_net():
    global _face_net
    if _face_net is None:
        with _face_net_lock:
            if _face_net is None:
                import cv2

                ensure_face_model()
                _face_net = cv2.dnn.readNetFromCaffe(str(PROTOTXT_PATH), str(CAFFEMODEL_PATH))
                logger.debug("Face detector network loaded.")
    return _face_net


def detect_face_center_x_in_frame(frame_path: Path, frame_width: int):
    import cv2
    import numpy as np

    img = cv2.imread(str(frame_path))
    if img is None:
        return None
    img_height, img_width = img.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0))
    net = get_face_net()
    net.setInput(blob)
    detections = net.forward()

    best_confidence = 0.0
    best_center_x = None
    threshold = settings.FACE_CONF_THRESHOLD
    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence < threshold:
            continue
        box = detections[0, 0, i, 3:7] * np.array([img_width, img_height, img_width, img_height])
        start_x, _, end_x, _ = box
        center_x = (start_x + end_x) / 2
        if confidence > best_confidence:
            best_confidence = confidence
            best_center_x = center_x

    if best_center_x is None:
        return None
    return (best_center_x / img_width) * frame_width


def detect_face_center_x(input_path: Path, start_sec: float, end_sec: float, frame_width: int) -> float:
    logger.info("Running face detection over [%.1f, %.1f] for vertical crop...", start_sec, end_sec)
    t0 = time.perf_counter()
    # Reduced from 5 to 3 samples for speed; still covers clip well.
    num_samples = settings.FACE_DETECT_SAMPLES or 3
    sample_times = [
        start_sec + (end_sec - start_sec) * (i + 1) / (num_samples + 1) for i in range(num_samples)
    ]
    frame_tag = uuid.uuid4().hex[:8]
    frame_paths = [
        TMP_DIR / f"frame-{os.getpid()}-{frame_tag}-{int(t * 1000)}.jpg"
        for t in sample_times
    ]

    def _grab_frame(args) -> Path | None:
        t, frame_path = args
        try:
            # Some ffmpeg builds reject -vsync here; keeping the single-frame capture
            # without it is sufficient and works across Windows/Linux builds.
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(input_path), "-frames:v", "1", str(frame_path)])
            return frame_path
        except Exception:
            logger.exception("Frame grab failed at t=%.1f", t)
            return None

    # Parallelize ffmpeg frame grabs (subprocess/IO-bound); face detection
    # itself stays sequential because the shared OpenCV DNN Net is not
    # guaranteed thread-safe.
    with ThreadPoolExecutor(max_workers=min(num_samples, 3)) as pool:
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

    logger.info(
        "Face detected in %d/%d sampled frames in %.2fs",
        len(detected_centers), num_samples, elapsed,
    )
    try:
        import numpy as np

        return float(np.median(detected_centers))
    except ImportError:
        return float(sum(detected_centers) / len(detected_centers))
