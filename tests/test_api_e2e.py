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
import struct
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

from test_ads_stack import BASE_DIR, VALID_TEXT, load_isolated_modules

ADMIN_KEY = "e2e-admin"
MEDIA_KEY = "e2e-media"
# A PNG header declaring 2x3 pixels: enough for the size and the sniffed type.
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 2, 3) + b"\x08\x02\x00\x00\x00"
SEEDED_URL = jsonlib.loads((BASE_DIR / "images.json").read_text())[0]["url"]
ROUTES = {"/accounts", "/accounts/{account_id}", "/accounts/{account_id}/ads",
          "/accounts/{account_id}/images", "/accounts/{account_id}/videos", "/ads/{ad_id}",
          "/videos/{video_id}", "/accounts/{account_id}/images/lookup",
          "/accounts/{account_id}/media/{kind}/{media_hash}"}


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


def fetch(url, token=None):
    """Return (status, Content-Type, body bytes) for a GET; HTTP errors are returned."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=10) as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers["Content-Type"], exc.read()


class ApiEndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-api-e2e-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def start_server(self, data_dir, admin_key=ADMIN_KEY, media_key=None):
        """Start uvicorn on a free port and return (process, base URL) once it answers."""
        # ponytail: small race between closing this socket and uvicorn binding it; retry if
        # CI flakes.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, ADS_STACK_DATA_DIR=str(data_dir), PYTHONDONTWRITEBYTECODE="1")
        env.pop("ADS_ADMIN_KEY", None)
        env.pop("ADS_MEDIA_KEY", None)
        if admin_key is not None:
            env["ADS_ADMIN_KEY"] = admin_key
        if media_key is not None:
            env["ADS_MEDIA_KEY"] = media_key
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

    def test_preview_urls_from_the_environment_key(self):
        _, url = self.start_server(self.data_dir, media_key="e2e-media")
        token = self.create_account(url)
        content = b"\x00\x00\x00\x18ftypmp42" + bytes(range(64))
        status, body = request("POST", f"{url}/accounts/1/videos?filename=v.mp4&name=Promo",
                               data=content, token=token)
        self.assertEqual(status, 201, body)
        status, video = request("GET", f"{url}/videos/1", token=token)
        # The URL is absolute, on the host the client called.
        self.assertTrue(video["preview_url"].startswith(f"{url}/accounts/1/media/videos/"), video)
        ranged = urllib.request.Request(video["preview_url"], headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(ranged, timeout=10) as response:
            self.assertEqual((response.status, response.headers["Content-Type"], response.read()),
                             (206, "video/mp4", content[10:20]))
        # A tampered signature is rejected.
        status, body = request("GET", video["preview_url"].replace("sig=", "sig=0"))
        self.assertEqual((status, body["error_code"]), (404, "002"))

    def upload_image(self, url, account_id, token, content):
        status, body = request("POST", f"{url}/accounts/{account_id}/images?filename=a.png",
                               data=content, token=token)
        self.assertEqual(status, 201, body)
        return body["hash"]

    def assert_not_found(self, response):
        status, _, body = response
        self.assertEqual(status, 404, body[:64])
        self.assertEqual(jsonlib.loads(body), {
            "error_code": "002", "error_message": "The value provided for media is invalid"})

    def test_media_is_served_only_to_the_owning_account(self):
        _, url = self.start_server(self.data_dir, media_key=MEDIA_KEY)
        token, other_token = self.create_account(url), self.create_account(url, 2)
        mine = self.upload_image(url, 1, token, PNG)
        theirs = self.upload_image(url, 2, other_token, b"theirs")
        # The account's own token fetches the bytes with no signature.
        self.assertEqual(fetch(f"{url}/accounts/1/media/images/{mine}", token),
                         (200, "image/png", PNG))
        # Another account's token, or a wrong one, is a 401.
        for wrong in (other_token, "wrong"):
            with self.subTest(token=wrong):
                status, _, body = fetch(f"{url}/accounts/1/media/images/{mine}", wrong)
                self.assertEqual(status, 401, body[:64])
                self.assertEqual(jsonlib.loads(body)["error_code"], "002")
        # Bytes stored for account 2 are missing to account 1, by token or by its signed URL.
        self.assert_not_found(fetch(f"{url}/accounts/1/media/images/{theirs}", token))
        _, image = request("GET", f"{url}/accounts/2/images/lookup?image_hash={theirs}",
                           token=other_token)
        self.assertEqual(fetch(image["preview_url"])[0], 200)
        self.assert_not_found(fetch(image["preview_url"].replace("/accounts/2/", "/accounts/1/")))

    def test_preview_urls_shared_across_processes_until_token_reissued(self):
        _, url = self.start_server(self.data_dir, media_key=MEDIA_KEY)
        token = self.create_account(url)
        image_hash = self.upload_image(url, 1, token, PNG)
        _, image = request("GET", f"{url}/accounts/1/images/lookup?image_hash={image_hash}",
                           token=token)
        preview_url = image["preview_url"]
        # A second server process with the same key, like another worker, accepts the URL;
        # one with a different key does not.
        _, same_key = self.start_server(self.data_dir, media_key=MEDIA_KEY)
        _, other_key = self.start_server(self.data_dir, media_key="other-media")
        on = lambda server: preview_url.replace(url, server)
        self.assertEqual(fetch(on(same_key)), (200, "image/png", PNG))
        self.assert_not_found(fetch(on(other_key)))
        # Reissuing the token from another process revokes the URL on every running server.
        load_isolated_modules(self, self.data_dir)
        new_token = self.service.AdsService(
            self.repository.SQLiteAdsRepository(self.publish.DB_PATH)).issue_access_token(1)
        for server in (url, same_key):
            with self.subTest(server=server):
                self.assert_not_found(fetch(on(server)))
                status, _, _ = fetch(f"{server}/accounts/1/media/images/{image_hash}", token)
                self.assertEqual(status, 401)
        _, image = request("GET", f"{url}/accounts/1/images/lookup?image_hash={image_hash}",
                           token=new_token)
        self.assertEqual(fetch(image["preview_url"]), (200, "image/png", PNG))

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
