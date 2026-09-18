import logging
import subprocess
import time

logger = logging.getLogger(__name__)


def run(command: list, timeout: float | None = 600) -> None:
    logger.debug("Running command: %s", " ".join(command))
    t0 = time.perf_counter()
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        # Keep head (input errors: crop>width, subtitles font) + tail
        # (encode errors) — tail-only truncation hid the real cause.
        raw = (e.stderr or b"").decode(errors="ignore")
        if len(raw) > 8000:
            stderr = raw[:4000] + "\n...[truncated]...\n" + raw[-4000:]
        else:
            stderr = raw
        logger.error("Command failed (%s): %s\n%s", command[0], " ".join(command), stderr)
        raise
    logger.debug("Command finished in %.2fs: %s", time.perf_counter() - t0, command[0])
