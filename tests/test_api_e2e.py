"""End-to-end HTTP API checks: a real uvicorn server process, called over sockets.

These cover what the in-process TestClient cannot: real chunked and Content-Length
handling, configuration from the environment, and persistence across a restart.
"""

import hashlib
import http.client
import json as jsonlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

try:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401
except ImportError:
    raise unittest.SkipTest("fastapi and uvicorn are not installed; see requirements.txt")

from test_ads_stack import BASE_DIR, VALID_TEXT

ADMIN_KEY = "e2e-admin"
SEEDED_URL = jsonlib.loads((BASE_DIR / "images.json").read_text())[0]["url"]
ROUTES = {"/accounts", "/accounts/{account_id}", "/accounts/{account_id}/ads",
          "/accounts/{account_id}/images", "/accounts/{account_id}/videos", "/ads/{ad_id}",
          "/videos/{video_id}"}


def request(method, url, *, json=None, data=None, token=None):
    """Return (status, parsed JSON body); HTTP errors are returned, not raised."""
    headers = {}
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


class ApiEndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-api-e2e-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def start_server(self, data_dir, admin_key=ADMIN_KEY):
        """Start uvicorn on a free port and return (process, base URL) once it answers."""
        # ponytail: small race between closing this socket and uvicorn binding it; retry if
        # CI flakes.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, ADS_STACK_DATA_DIR=str(data_dir), PYTHONDONTWRITEBYTECODE="1")
        env.pop("ADS_ADMIN_KEY", None)
        if admin_key is not None:
            env["ADS_ADMIN_KEY"] = admin_key
        stderr = tempfile.TemporaryFile()
        self.addCleanup(stderr.close)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=stderr)
        self.addCleanup(self.stop_server, process)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(f"{url}/openapi.json", timeout=1).close()
                return process, url
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    stderr.seek(0)
                    self.fail(f"server did not start:\n{stderr.read().decode()}")
                time.sleep(0.05)

    @staticmethod
    def stop_server(process):
        process.terminate()
        process.wait(timeout=5)

    def create_account(self, url, account_id=1):
        status, body = request("POST", f"{url}/accounts", json={"id": account_id, "name": "A"},
                               token=ADMIN_KEY)
        self.assertEqual(status, 201, body)
        return body["access_token"]

    def test_full_publishing_flow(self):
        _, url = self.start_server(self.data_dir)
        token = self.create_account(url)
        sample = (BASE_DIR / "file_handle_store" / "sample.jpg").read_bytes()
        query = urllib.parse.urlencode({"filename": "sample.jpg", "image_name": "hero"})
        status, body = request("POST", f"{url}/accounts/1/images?{query}", data=sample,
                               token=token)
        self.assertEqual((status, body), (201, {"hash": hashlib.sha256(sample).hexdigest()[:32]}))
        image_hash = body["hash"]

        status, body = request("POST", f"{url}/accounts/1/ads",
                               json={"text": VALID_TEXT, "image_hash": image_hash}, token=token)
        self.assertEqual((status, body), (201, {"id": 1}))
        status, body = request("POST", f"{url}/accounts/1/ads",
                               json={"text": VALID_TEXT, "image_name": "hero"}, token=token)
        self.assertEqual((status, body), (201, {"id": 2}))
        status, body = request("GET", f"{url}/ads/1?fields=text,image_hash")
        self.assertEqual((status, body), (200, {"text": VALID_TEXT, "image_hash": image_hash}))

        status, first = request("GET", f"{url}/accounts/1/ads?limit=1")
        self.assertEqual((status, [ad["id"] for ad in first["data"]]), (200, [2]))
        cursor = urllib.parse.quote(first["next_cursor"])
        status, second = request("GET", f"{url}/accounts/1/ads?limit=1&cursor={cursor}")
        self.assertEqual([ad["id"] for ad in second["data"]], [1])

        status, body = request("POST", f"{url}/accounts/1/videos?filename=v.mp4&name=Promo",
                               data=b"video", token=token)
        self.assertEqual((status, body), (201, {"id": 1}))
        status, body = request("POST", f"{url}/accounts/1/ads",
                               json={"text": VALID_TEXT, "video_id": 1}, token=token)
        self.assertEqual((status, body), (201, {"id": 3}))
        status, body = request("PATCH", f"{url}/videos/1", json={"name": "Promo 2"}, token=token)
        self.assertEqual((status, body["name"]), (200, "Promo 2"))
        status, body = request("GET", f"{url}/accounts/1/videos", token=token)
        self.assertEqual((status, [video["name"] for video in body["data"]]), (200, ["Promo 2"]))

    def test_data_survives_server_restart(self):
        process, url = self.start_server(self.data_dir)
        token = self.create_account(url)
        status, _ = request("POST", f"{url}/accounts/1/ads",
                            json={"text": VALID_TEXT, "image": SEEDED_URL}, token=token)
        self.assertEqual(status, 201)
        status, before = request("GET", f"{url}/ads/1")
        self.assertEqual(status, 200)
        self.stop_server(process)

        _, url = self.start_server(self.data_dir)
        self.assertEqual(request("GET", f"{url}/ads/1"), (200, before))
        status, body = request("PATCH", f"{url}/accounts/1", json={"name": "Renamed"},
                               token=token)
        self.assertEqual((status, body), (200, {"id": 1, "name": "Renamed"}))

    def test_chunked_upload_without_content_length_rejected(self):
        _, url = self.start_server(self.data_dir)
        token = self.create_account(url)
        connection = http.client.HTTPConnection(urllib.parse.urlsplit(url).netloc, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/accounts/1/images?filename=a.png", body=iter([b"abc"]),
                           headers={"Authorization": f"Bearer {token}",
                                    "Transfer-Encoding": "chunked"},
                           encode_chunked=True)
        response = connection.getresponse()
        self.assertEqual(response.status, 411)
        self.assertEqual(jsonlib.loads(response.read()),
                         {"error_code": "002",
                          "error_message": "The value provided for file is invalid"})
        uploads = self.data_dir / "uploads"
        self.assertEqual(list(uploads.iterdir()) if uploads.exists() else [], [])

    def test_admin_key_not_configured_blocks_account_creation(self):
        _, url = self.start_server(self.data_dir, admin_key=None)
        status, body = request("POST", f"{url}/accounts", json={"id": 1, "name": "A"},
                               token="anything")
        self.assertEqual((status, body["error_code"]), (401, "002"))
        self.assertEqual(request("GET", f"{url}/accounts/1")[0], 404)

    def test_openapi_lists_every_route(self):
        _, url = self.start_server(self.data_dir)
        status, body = request("GET", f"{url}/openapi.json")
        self.assertEqual((status, set(body["paths"])), (200, ROUTES))


if __name__ == "__main__":
    unittest.main()
