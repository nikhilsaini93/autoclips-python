import asyncio
import logging
import shutil

from app.config import CLIPS_DIR, DOWNLOAD_DIR, TMP_DIR, VIDEOS_DIR

logger = logging.getLogger(__name__)


def delete_all_files_sync():
    """Blocking filesystem cleanup — run via asyncio.to_thread."""
    deleted_count = 0
    freed_bytes = 0

    folders = [DOWNLOAD_DIR, CLIPS_DIR, TMP_DIR, VIDEOS_DIR]

    for folder in folders:
        if not folder.exists():
            continue

        for item in list(folder.iterdir()):
            try:
                if item.is_file() or item.is_symlink():
                    if item.is_file():
                        freed_bytes += item.stat().st_size
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    for p in item.rglob("*"):
                        if p.is_file():
                            try:
                                freed_bytes += p.stat().st_size
                            except OSError:
                                pass
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception:
                logger.exception("Failed to delete %s", item)

    return deleted_count, freed_bytes


async def delete_all_files():
    return await asyncio.to_thread(delete_all_files_sync)
