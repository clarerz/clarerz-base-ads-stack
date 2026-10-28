"""Size-capped, content-addressed file copies shared by image and video storage."""

import hashlib
import os
from pathlib import Path
import tempfile


def store_file(path, directory, max_bytes, suffix="") -> str:
    """Copy a local file into directory as <hash><suffix> and return its 32-character hash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    tmp = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    try:
        # Hash while copying, then rename, so a failed or oversized copy never leaves a
        # partial file. Counting copied bytes enforces the cap even if the source grows.
        with open(path, "rb") as src, tmp:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"file exceeds {max_bytes} bytes")
                digest.update(chunk)
                tmp.write(chunk)
        file_hash = digest.hexdigest()[:32]
        # Identical content maps to the same name, so re-uploads deduplicate.
        os.replace(tmp.name, directory / f"{file_hash}{suffix}")
    except BaseException:
        os.unlink(tmp.name)
        raise
    return file_hash
