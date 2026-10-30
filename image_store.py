"""Local image registry with seeded metadata and persistent file ingestion.

URL ingestion records metadata using a URL-derived hash; it does not download
image bytes. Local files are copied and hashed by content.
"""

from contextlib import contextmanager, suppress
import fcntl
import hashlib
import json
import os
import struct
import tempfile
from typing import Dict, List, Optional, Tuple

import config
import file_store

_SEEDED_PATH = os.path.join(os.path.dirname(__file__), "images.json")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PERSIST_DIR = os.path.join(config.DATA_DIR, "image_store_data")
_PERSIST_FILE = os.path.join(_PERSIST_DIR, "persisted_images.json")
_LOCK_FILE = os.path.join(config.DATA_DIR, "image_registry.lock")
MAX_IMAGE_BYTES = 30 * 1024 * 1024  # Meta's ad image maximum

# Image data is stored separately from the ads database.
os.makedirs(_PERSIST_DIR, exist_ok=True)


def _load_seeded() -> List[Dict]:
    if not os.path.exists(_SEEDED_PATH):
        return []
    # A damaged seed file is an error: treating it as empty would hide the seeded images.
    try:
        with open(_SEEDED_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"seed image file unreadable: {exc}") from exc


def _load_persisted() -> List[Dict]:
    if not os.path.exists(_PERSIST_FILE):
        return []
    # Fail on read and parse errors: treating a damaged registry as empty would let
    # the next ingestion overwrite every other image's record. RuntimeError, not the
    # parser's ValueError, because ValueError means unusable caller input.
    try:
        with open(_PERSIST_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"image registry unreadable: {exc}") from exc


def _save_persisted(images: List[Dict]) -> None:
    # Propagate failures so publishing cannot report a non-durable image as saved.
    # Write a synced temporary file and rename it over the registry, so a crash
    # leaves the old or new registry, never a partial one.
    tmp = tempfile.NamedTemporaryFile("w", dir=_PERSIST_DIR, suffix=".tmp", delete=False)
    try:
        with tmp:
            json.dump(images, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, _PERSIST_FILE)
    except BaseException:
        os.unlink(tmp.name)
        raise


@contextmanager
def _registry_lock():
    # ponytail: one exclusive flock (Unix only) serializes every registry write, and
    # lookups still scan the whole JSON file; move the registry into SQLite if writes
    # contend or it grows large.
    with open(_LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield  # Closing the file releases the lock.


def _register(entry: Dict, key: str) -> Dict:
    """Persist entry unless an image with the same key is known; return the stored record."""
    # Reload under the lock: a concurrent writer may have saved since the caller looked.
    with _registry_lock():
        persisted = _load_persisted()
        for img in _load_seeded():
            if img.get(key) == entry[key]:
                return img
        for img in persisted:
            if img.get(key) == entry[key]:
                # Entries stored before sizes were recorded learn them on re-upload.
                if img.get("source") != "url" and not _has_size(img) and _has_size(entry):
                    img.update(width=entry["width"], height=entry["height"])
                    _save_persisted(persisted)
                return img
        persisted.append(entry)
        _save_persisted(persisted)
    return entry


def _has_size(entry: Dict) -> bool:
    return all(type(entry.get(side)) is int and entry[side] > 0 for side in ("width", "height"))


def _drop_duplicate_copy(registered: Optional[str], copy: str) -> None:
    """Delete copy when the registry already keeps these bytes in another file."""
    # Compare files, not path strings: a re-upload with the same suffix, or one through a
    # symlinked directory, rewrote the registered file itself and must not delete it. A
    # registered file outside this store (a data directory copied elsewhere) may later
    # disappear, so the local copy is kept.
    # Cleanup is best effort: a concurrent upload may have removed the copy already, and a
    # leftover copy is harmless, so neither may fail an upload that registered.
    with suppress(OSError):
        if (registered and os.path.exists(registered)
                and os.path.samefile(os.path.dirname(registered), _PERSIST_DIR)
                and not os.path.samefile(registered, copy)):
            os.unlink(copy)


def image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) from a PNG, GIF, JPEG, or WEBP header, or None if unknown."""
    # Header parsing only: a file can declare a size and still fail to decode.
    try:
        with open(path, "rb") as f:
            head = f.read(30)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                size = struct.unpack(">II", head[16:24])
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                size = struct.unpack("<HH", head[6:10])
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                size = _webp_dimensions(head)
            elif head[:2] == b"\xff\xd8":
                size = _jpeg_dimensions(f)
            else:
                size = None
    except (OSError, struct.error):
        return None
    return size if size and min(size) > 0 else None


def _webp_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    chunk = head[12:16]
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":  # Lossy: 14-bit sizes.
        width, height = struct.unpack("<HH", head[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and head[20:21] == b"\x2f":  # Lossless: packed (size - 1) fields.
        bits = struct.unpack("<I", head[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(head) >= 30:  # Extended: 24-bit (size - 1) fields.
        return (int.from_bytes(head[24:27], "little") + 1,
                int.from_bytes(head[27:30], "little") + 1)
    return None


# Start-of-frame markers carry the size; C4, C8, and CC share the range but do not.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_MAX_STEPS = 4096


def _jpeg_dimensions(f) -> Optional[Tuple[int, int]]:
    f.seek(2)
    # Real files reach the frame header within a few dozen markers; the cap bounds the work a
    # crafted file of fill bytes or empty markers can cause.
    for _ in range(_JPEG_MAX_STEPS):
        if f.read(1) != b"\xff":
            return None
        marker = f.read(1)
        if marker == b"\xff":  # A fill byte: the next 0xFF may start the marker.
            f.seek(-1, os.SEEK_CUR)
            continue
        if not marker or marker[0] == 0xDA:  # End of file, or image data before any frame.
            return None
        if 0xD0 <= marker[0] <= 0xD9 or marker[0] == 0x01:  # Markers without a length.
            continue
        length = struct.unpack(">H", f.read(2))[0]
        if length < 2:
            return None
        if marker[0] in _JPEG_SOF:
            _, height, width = struct.unpack(">BHH", f.read(5))
            return width, height
        f.seek(length - 2, os.SEEK_CUR)
    return None


def _hash_for_url(url: str) -> str:
    # Deterministic alphanumeric hash for URLs (32-char hex)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def list_images() -> List[Dict]:
    """Return all images known to the existing image storage (seeded + persisted)."""
    return _load_seeded() + _load_persisted()


def get_image_by_url(url: str) -> Optional[Dict]:
    for img in list_images():
        if img.get("url") == url:
            return img
    return None


def get_image_by_hash(hash_val: str) -> Optional[Dict]:
    for img in list_images():
        if img.get("hash") == hash_val:
            return img
    return None


def store_image(image_input: str) -> Dict:
    """
    Store/retrieve image from URL or file handle path.

    - If image_input looks like URL (http/https), validate and return existing or create persisted entry.
    - If image_input looks like a file path that exists locally, ingest it (copy to _PERSIST_DIR), hash it, and return entry.
    - Raises ValueError for invalid input (caller should map to 002).
    - Raises RuntimeError for storage failures (caller should map to 003).
    """
    if not image_input or not isinstance(image_input, str):
        raise ValueError("image input must be non-empty string url or file path")

    # URL path
    if image_input.startswith("http://") or image_input.startswith("https://"):
        # Check if already known
        existing = get_image_by_url(image_input)
        if existing:
            return existing
        # Create new persisted entry for this URL
        new_id = f"img_{_hash_for_url(image_input)[:8]}"
        entry = {
            "id": new_id,
            "url": image_input,
            "hash": _hash_for_url(image_input),
            "width": 0,
            "height": 0,
            "source": "url",
        }
        try:
            return _register(entry, "url")
        except Exception as e:
            raise RuntimeError(f"transient persist failure: {e}")

    # Support absolute paths, paths relative to this module, and sample filenames.
    candidate_paths = [
        image_input,
        os.path.join(_BASE_DIR, image_input),
        os.path.join(_BASE_DIR, "file_handle_store", image_input),
    ]
    real_path = None
    for p in candidate_paths:
        if os.path.exists(p) and os.path.isfile(p):
            real_path = p
            break

    if real_path is None:
        # If it contains a slash but doesn't exist, treat as invalid value (not transient)
        if "/" in image_input or "\\" in image_input:
            raise ValueError(f"file handle not found: {image_input}")
        raise ValueError(
            f"Invalid image input (neither URL nor existing file path): {image_input}"
        )

    try:
        # Copy and hash in one pass; files over MAX_IMAGE_BYTES raise ValueError.
        suffix = os.path.splitext(real_path)[1] or ".bin"
        file_hash = file_store.store_file(real_path, _PERSIST_DIR, MAX_IMAGE_BYTES, suffix)
        dest_path = os.path.join(_PERSIST_DIR, f"{file_hash}{suffix}")

        new_id = f"img_{file_hash[:8]}"
        entry = {
            "id": new_id,
            "url": None,
            "hash": file_hash,
            "path": dest_path,
            "original_path": real_path,
            "source": "file_handle",
        }
        entry["width"], entry["height"] = image_dimensions(dest_path) or (None, None)
        stored = _register(entry, "hash")
        _drop_duplicate_copy(stored.get("path"), dest_path)
        return stored
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"transient file ingest failure: {e}")
