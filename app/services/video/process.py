import logging
import subprocess
import time

logger = logging.getLogger(__name__)


def run(command: list) -> None:
    logger.debug("Running command: %s", " ".join(command))
    t0 = time.perf_counter()
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[-2000:]
        logger.error("Command failed (%s): %s\n%s", command[0], " ".join(command), stderr)
        raise
    logger.debug("Command finished in %.2fs: %s", time.perf_counter() - t0, command[0])
