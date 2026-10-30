"""Local video storage: uploaded files are copied in and addressed by content hash."""

import config
import file_store

VIDEO_DIR = config.DATA_DIR / "videos"
MAX_VIDEO_BYTES = 1024 ** 3  # 1 GiB


def store_video(path) -> str:
    """Copy a local file of at most MAX_VIDEO_BYTES into storage and return its hash handle."""
    return file_store.store_file(path, VIDEO_DIR, MAX_VIDEO_BYTES)
