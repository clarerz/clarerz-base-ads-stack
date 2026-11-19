"""Shared test setup: isolated module loading, fixtures, and expected error bodies."""

import importlib.util
import json as jsonlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parents[1]
VALID_TEXT = "Summer sale! Get 50% off today only - limited time offer"
MODULE_NAMES = ("config", "models", "repository", "validation", "file_store", "image_store",
                "video_store", "publishing", "service", "publish")
# A PNG header declaring 2x3 pixels: enough for the size and the sniffed type.
PNG = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 2, 3)
       + b"\x08\x02\x00\x00\x00")

# Every test file imports this module. Temporary storage goes to tmpfs unless TMPDIR is set:
# SQLite syncs every commit to disk, which made up most of the suite's run time.
if "TMPDIR" not in os.environ and os.access("/dev/shm", os.W_OK):
    tempfile.tempdir = "/dev/shm"


def load_isolated_modules(test_case, data_dir, names=MODULE_NAMES):
    """Load fresh copies of the modules on test_case, storing data in data_dir."""
    env = patch.dict(os.environ, {"ADS_STACK_DATA_DIR": str(data_dir)})
    env.start()
    test_case.addCleanup(env.stop)
    modules = patch.dict(sys.modules)
    modules.start()
    test_case.addCleanup(modules.stop)
    # Load fresh modules after setting storage, without creating project data.
    for name in names:
        spec = importlib.util.spec_from_file_location(name, BASE_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        setattr(test_case, name, module)


def error_body(code, field):
    """The error dict the API returns for code and field."""
    message = {"001": f"{field} is an invalid field on this endpoint",
               "003": f"Storage failed while handling {field}"}.get(
                   code, f"The value provided for {field} is invalid")
    return {"error_code": code, "error_message": message}


def request(method, url, *, json=None, data=None, token=None, headers=None):
    """Return (status, parsed JSON body) over a real socket; HTTP errors are returned."""
    headers = dict(headers or {})
    if json is not None:
        # urllib would otherwise send form encoding, which is not a JSON body.
        data = jsonlib.dumps(json).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, jsonlib.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, jsonlib.loads(exc.read() or b"null")
