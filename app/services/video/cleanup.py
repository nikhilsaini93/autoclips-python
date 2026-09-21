import logging
import time
from pathlib import Path

from app.config import CLIPS_DIR, DOWNLOAD_DIR, TMP_DIR, VIDEOS_DIR

logger = logging.getLogger(__name__)


def cleanup_old_files(max_age_hours: float = 72, max_total_mb: float | None = None) -> dict:
    """Delete files older than max_age_hours in storage/downloads|clips|tmp|videos.

    Optionally enforces a total size cap (oldest first) when max_total_mb is set.
    Returns {"deleted": N, "freed_mb": X}. Never deletes the folders themselves.
    """
    import shutil as _shutil

    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    freed = 0

    folders = [DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, VIDEOS_DIR]
    candidates: list[Path] = []
    for folder in folders:
        if not folder.exists():
            continue
        for item in folder.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    candidates.append(item)
                elif item.is_dir():
                    # Count nested dirs as one candidate by oldest mtime inside
                    try:
                        mtime = min(
                            (p.stat().st_mtime for p in item.rglob("*") if p.is_file()),
                            default=item.stat().st_mtime,
                        )
                    except OSError:
                        mtime = item.stat().st_mtime
                    candidates.append(item)
            except OSError:
                continue

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def _size(p: Path) -> int:
        try:
            if p.is_file():
                return p.stat().st_size
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return 0

    # 1) Age-based purge
    for item in list(candidates):
        try:
            if _mtime(item) < cutoff:
                freed += _size(item)
                if item.is_file() or item.is_symlink():
                    item.unlink()
                else:
                    _shutil.rmtree(item)
                deleted += 1
                candidates.remove(item)
        except OSError:
            logger.exception("Cleanup failed for %s", item)

    # 2) Optional total-size cap, oldest first
    if max_total_mb is not None:
        total_mb = sum(_size(p) for p in candidates) / (1024 * 1024)
        for item in sorted(candidates, key=_mtime):
            if total_mb <= max_total_mb:
                break
            try:
                size_mb = _size(item) / (1024 * 1024)
                if item.is_file() or item.is_symlink():
                    item.unlink()
                else:
                    _shutil.rmtree(item)
                deleted += 1
                freed += int(size_mb * 1024 * 1024)
                total_mb -= size_mb
            except OSError:
                logger.exception("Cleanup (size cap) failed for %s", item)

    logger.info("Cleanup: deleted %d item(s), freed %.1f MB", deleted, freed / (1024 * 1024))
    return {"deleted": deleted, "freed_mb": round(freed / (1024 * 1024), 1)}
