"""Persistent ad accounts and single-image ads, exposed as Python functions."""

import os
import re
import sqlite3
import time

import image_store

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("ADS_STACK_DATA_DIR", os.path.join(_BASE_DIR, "data"))
))
DB_PATH = os.path.join(_DATA_DIR, "ads.db")

# An image can be a URL or a local file path; image_hash accepts an existing handle.
IMAGE_KEYS = {"image"}
HASH_KEYS = {"image_hash"}
TEXT_KEYS = {"text"}
CREATOR_KEYS = {"creator_id"}
LEGAL_KEYS = IMAGE_KEYS | HASH_KEYS | TEXT_KEYS | CREATOR_KEYS

URL_REGEX = re.compile(r"^https?://[^\s]+$")
HASH_REGEX = re.compile(r"^[A-Za-z0-9]{16,64}$")

# File-handle values may be given as a bare name or a path; probe these roots.
_FILE_SEARCH_ROOTS = ("", _BASE_DIR, os.path.join(_BASE_DIR, "file_handle_store"))


def _error(code, field=None):
    if code == "001":
        msg = f"{field} is an invalid field on this endpoint"
    else:  # "002"
        msg = f"The value provided for {field} is invalid"
    return {"error_code": code, "error_message": msg}


def _connect():
    """Open the DB, lazily creating its storage directory and schema."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accounts ( id INTEGER PRIMARY KEY, name TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ads ("
        " id INTEGER PRIMARY KEY,"
        " system_name TEXT,"
        " image_hash TEXT,"
        " text_content TEXT,"
        " creator_id INTEGER,"
        " creation_time INTEGER"
        ")"
    )
    return conn


def _coerce_account_id(value):
    """Return an int account id from an int or a digit-string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def create_account(account_id, name):
    """Create the ad-object owner (numeric id + string name)."""
    cid = _coerce_account_id(account_id)
    if cid is None:
        return _error("002", "account_id")
    if not isinstance(name, str) or not name.strip():
        return _error("002", "name")
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO accounts (id, name) VALUES (?, ?)",
                (cid, name.strip()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return _error("002", "account")
    return {"id": cid}


def _account_exists(account_id):
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    return row is not None


def get_account(account_id):
    """Read path for ad account – return persisted account with both fields, or None."""
    cid = _coerce_account_id(account_id)
    if cid is None:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, name FROM accounts WHERE id = ?", (cid,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    return {"id": row[0], "name": row[1]}


def _resolve_file(value):
    """Return an existing file path for a file-handle value, or None."""
    for root in _FILE_SEARCH_ROOTS:
        candidate = os.path.join(root, value) if root else value
        if os.path.isfile(candidate):
            return candidate
    base = os.path.basename(value)
    candidate = os.path.join(_BASE_DIR, "file_handle_store", base)
    return candidate if os.path.isfile(candidate) else None


def _store_image(value):
    """Resolve an image reference to a content handle (hash).

    Returns (hash, error_code); exactly one is non-None. A URL is normalized to
    its content hash (never persisted as a URL); a file handle is resolved and
    hashed. error_code is "002" on a bad value.
    """
    if value.startswith("http://") or value.startswith("https://"):
        if not URL_REGEX.match(value):
            return None, "002"
        target = value
    else:
        real_path = _resolve_file(value)
        if real_path is None:
            return None, "002"
        target = real_path

    try:
        stored = image_store.store_image(target)
        return stored.get("hash"), None
    except Exception:
        return None, "002"


def publish_ad(data: dict) -> dict:
    if not isinstance(data, dict):
        return _error("002", "data")

    # Reject unknown fields.
    for key in data:
        if key not in LEGAL_KEYS:
            return _error("001", key)

    text_fields = [k for k in data if k in TEXT_KEYS]
    image_fields = [k for k in data if k in IMAGE_KEYS]
    hash_fields = [k for k in data if k in HASH_KEYS]
    creator_fields = [k for k in data if k in CREATOR_KEYS]

    # Exactly one of each role (creator + text required; image or hash required).
    if len(text_fields) == 0:
        return _error("002", "text")
    if len(text_fields) > 1:
        return _error("001", text_fields[1])
    if len(image_fields) > 1:
        return _error("001", image_fields[1])
    if len(hash_fields) > 1:
        return _error("001", hash_fields[1])
    if len(creator_fields) > 1:
        return _error("001", creator_fields[1])
    if not image_fields and not hash_fields:
        return _error("002", "image")
    if len(creator_fields) == 0:
        return _error("002", "creator_id")

    # Creator must be numeric AND reference an existing account.
    creator_key = creator_fields[0]
    cid = _coerce_account_id(data[creator_key])
    if cid is None:
        return _error("002", creator_key)
    if not _account_exists(cid):
        return _error("002", creator_key)

    # Text must be human copy.
    text_key = text_fields[0]
    text_val = data[text_key]
    if not isinstance(text_val, str) or len(text_val.strip()) < 3:
        return _error("002", text_key)
    if URL_REGEX.match(text_val.strip()) or HASH_REGEX.match(text_val.strip()):
        return _error("002", text_key)
    text_val = text_val.strip()

    # Image -> normalized content handle (hash).
    image_hash = None
    if image_fields:
        image_key = image_fields[0]
        image_val = data[image_key]
        if not isinstance(image_val, str) or not image_val.strip():
            return _error("002", image_key)
        h, err_code = _store_image(image_val.strip())
        if err_code is not None:
            return _error(err_code, image_key)
        image_hash = h

    if hash_fields:
        hash_key = hash_fields[0]
        hash_val = data[hash_key]
        if not isinstance(hash_val, str) or not HASH_REGEX.match(hash_val.strip()):
            return _error("002", hash_key)
        if image_hash is None:
            image_hash = hash_val.strip()

    # Persist. The ad id is numeric; system_name follows the template "ad_<ad_id>".
    try:
        conn = _connect()
        try:
            ad_id = int(
                conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM ads").fetchone()[0]
            )
            system_name = f"ad_{ad_id}"
            creation_time = int(time.time())
            conn.execute(
                "INSERT INTO ads (id, system_name, image_hash, text_content, "
                "creator_id, creation_time) VALUES (?, ?, ?, ?, ?, ?)",
                (ad_id, system_name, image_hash, text_val, cid, creation_time),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return _error("002", "data")

    return {"id": ad_id}


def _fetch(ad_id):
    """Return the full persisted ad object, or None."""
    aid = _coerce_account_id(ad_id)  # generic numeric coercion (int / digit-string)
    if aid is None:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, system_name, image_hash, text_content, creator_id, "
                "creation_time FROM ads WHERE id = ?",
                (aid,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    # Image is exposed as a normalized file handle (content hash).
    return {
        "id": row[0],
        "system_name": row[1],
        "image_hash": row[2],
        "text": row[3],
        "creator_id": row[4],
        "creation_time": row[5],
    }


def get_ads(ad_id, fields=None):
    """Read path: return requested fields for an ad (validating field names)."""
    obj = _fetch(ad_id)
    if obj is None:
        return None
    if fields is None:
        return obj
    if isinstance(fields, str):
        fields = [fields]
    if not isinstance(fields, (list, tuple, set)):
        return _error("002", "fields")
    if len(fields) == 0:
        return obj
    for f in fields:
        if f not in obj:
            return _error("001", f)
    return {f: obj[f] for f in fields}


def get_ad(ad_id):
    """Backward-compatible read path: return the full ad object or None."""
    return _fetch(ad_id)
