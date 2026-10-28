"""Local image registry with seeded metadata and persistent file ingestion.

URL ingestion records metadata using a URL-derived hash; it does not download
image bytes. Local files are copied and hashed by content.
"""

import hashlib
import json
import os
from typing import Dict, List, Optional

import file_store

_SEEDED_PATH = os.path.join(os.path.dirname(__file__), "images.json")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("ADS_STACK_DATA_DIR", os.path.join(_BASE_DIR, "data"))
))
_PERSIST_DIR = os.path.join(_DATA_DIR, "image_store_data")
_PERSIST_FILE = os.path.join(_PERSIST_DIR, "persisted_images.json")
MAX_IMAGE_BYTES = 30 * 1024 * 1024  # Meta's ad image maximum

# Image data is stored separately from the ads database.
os.makedirs(_PERSIST_DIR, exist_ok=True)


def _load_seeded() -> List[Dict]:
    if not os.path.exists(_SEEDED_PATH):
        return []
    try:
        with open(_SEEDED_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _load_persisted() -> List[Dict]:
    if not os.path.exists(_PERSIST_FILE):
        return []
    try:
        with open(_PERSIST_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_persisted(images: List[Dict]) -> None:
    # Propagate failures so publishing cannot report a non-durable image as saved.
    with open(_PERSIST_FILE, "w") as f:
        json.dump(images, f, indent=2)


def _hash_for_url(url: str) -> str:
    # Deterministic alphanumeric hash for URLs (32-char hex)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def list_images() -> List[Dict]:
    """Return all images known to the existing image storage (seeded + persisted)."""
    return _load_seeded() + _load_persisted()


def get_image(identifier: str) -> Optional[Dict]:
    """Lookup by id, url, or hash."""
    if not identifier:
        return None
    for img in list_images():
        if identifier in (img.get("id"), img.get("url"), img.get("hash")):
            return img
    return None


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


def image_exists(identifier: str) -> bool:
    return get_image(identifier) is not None


def store_image(image_input: str) -> Dict:
    """
    Store/retrieve image from URL or file handle path.

    - If image_input looks like URL (http/https), validate and return existing or create persisted entry.
    - If image_input looks like a file path that exists locally, ingest it (copy to _PERSIST_DIR), hash it, and return entry.
    - Raises ValueError for invalid input (caller should map to 002).
    - Raises RuntimeError for transient failures (caller should map to 002).
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
            persisted = _load_persisted()
            persisted.append(entry)
            _save_persisted(persisted)
        except Exception as e:
            raise RuntimeError(f"transient persist failure: {e}")
        return entry

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
        # Deduplicate by hash
        existing = get_image_by_hash(file_hash)
        if existing:
            return existing
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
        persisted = _load_persisted()
        persisted.append(entry)
        _save_persisted(persisted)
        return entry
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"transient file ingest failure: {e}")


def validate_image_reference(
    url: Optional[str] = None, hash_val: Optional[str] = None
) -> bool:
    """Check that url and hash, if both provided, belong to same stored image."""
    if url and hash_val:
        img_by_url = get_image_by_url(url)
        img_by_hash = get_image_by_hash(hash_val)
        # Both must exist and be linked
        if not img_by_url or not img_by_hash:
            return False
        return (
            img_by_url.get("id") == img_by_hash.get("id")
            or img_by_url.get("hash") == hash_val
        )
    if url:
        return get_image_by_url(url) is not None
    if hash_val:
        return get_image_by_hash(hash_val) is not None
    return False
