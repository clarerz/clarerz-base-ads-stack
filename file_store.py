"""Size-capped, content-addressed file storage shared by image and video storage."""

import hashlib
import os
from pathlib import Path
import tempfile


def _hash_copy(src, max_bytes, out=None) -> str:
    """Return the 32-character hash of src, writing it to out if given; ValueError past max_bytes."""
    digest = hashlib.sha256()
    size = 0
    # Counting bytes read enforces the cap even if the source grows.
    for chunk in iter(lambda: src.read(1 << 20), b""):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} bytes")
        digest.update(chunk)
        if out is not None:
            out.write(chunk)
    return digest.hexdigest()[:32]


def store_file(path, directory, max_bytes):
    """Store a local file in directory under its content hash; return (hash, stored path).

    Content already in directory is found by hash and never written again, whatever name or
    extension it arrives under.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        src = open(path, "rb")
    except OSError as exc:
        # An unreadable or vanished source is the caller's input, not a storage failure.
        raise ValueError(f"cannot read {path}: {exc}") from exc
    with src:
        file_hash = _hash_copy(src, max_bytes)
        # Older builds stored images as <hash><suffix>, so any name starting with the hash
        # holds these bytes.
        existing = min(directory.glob(f"{file_hash}*"), default=None)
        if existing is not None:
            return file_hash, existing
        src.seek(0)
        tmp = tempfile.NamedTemporaryFile(dir=directory, delete=False)
        try:
            # Copy to a temporary file, then rename, so a failed or oversized copy never
            # leaves a partial file.
            with tmp:
                copied_hash = _hash_copy(src, max_bytes, tmp)
            if copied_hash != file_hash:
                # The name must match the bytes actually stored.
                raise ValueError(f"{path} changed while it was being stored")
            # The name depends only on content, so concurrent uploads of the same bytes
            # replace one another's copy instead of adding a second name.
            stored = directory / file_hash
            os.replace(tmp.name, stored)
        except BaseException:
            os.unlink(tmp.name)
            raise
    return file_hash, stored
