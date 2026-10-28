"""Local video storage: uploaded files are copied in and addressed by content hash."""

import os
from pathlib import Path

import file_store

_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ.get("ADS_STACK_DATA_DIR", str(_BASE_DIR / "data"))).expanduser().resolve()
VIDEO_DIR = _DATA_DIR / "videos"
MAX_VIDEO_BYTES = 1024 ** 3  # 1 GiB


def store_video(path) -> str:
    """Copy a local file of at most MAX_VIDEO_BYTES into storage and return its hash handle."""
    return file_store.store_file(path, VIDEO_DIR, MAX_VIDEO_BYTES)
