"""Local video storage: uploaded files are copied in and addressed by content hash."""

import hashlib
import os
from pathlib import Path
import tempfile

_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ.get("ADS_STACK_DATA_DIR", str(_BASE_DIR / "data"))).expanduser().resolve()
VIDEO_DIR = _DATA_DIR / "videos"


def store_video(path) -> str:
    """Copy a local file into storage and return its 32-character hash handle."""
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(dir=VIDEO_DIR, delete=False)
    try:
        # Hash while copying, then rename, so a failed copy never leaves a partial video.
        with open(path, "rb") as src, tmp:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                digest.update(chunk)
                tmp.write(chunk)
        video_hash = digest.hexdigest()[:32]
        # Identical content maps to the same handle, so re-uploads deduplicate.
        os.replace(tmp.name, VIDEO_DIR / video_hash)
    except BaseException:
        os.unlink(tmp.name)
        raise
    return video_hash
