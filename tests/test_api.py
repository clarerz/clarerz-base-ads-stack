"""HTTP API checks through FastAPI's in-process TestClient, on isolated storage."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from fastapi.testclient import TestClient
    from starlette.requests import ClientDisconnect
except ImportError:
    raise unittest.SkipTest("fastapi is not installed; see requirements.txt")

from test_ads_stack import BASE_DIR, MODULE_NAMES, VALID_TEXT, load_isolated_modules

ADMIN_KEY = "test-admin"


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-api-test-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        load_isolated_modules(self, self.data_dir, MODULE_NAMES + ("api",))
        # The environment is restored on cleanup; an exported key must not stand in for None.
        os.environ.pop("ADS_ADMIN_KEY", None)
        self.client = TestClient(self.api.create_app(admin_key=ADMIN_KEY))
        self.token = self.create_account(1, "Advertiser One")
        self.seeded = json.loads((BASE_DIR / "images.json").read_text())

    def create_account(self, account_id, name):
        response = self.client.post("/accounts", json={"id": account_id, "name": name},
                                    headers=self.bearer(ADMIN_KEY))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def assert_error(self, response, status, code, field):
        self.assertEqual(response.status_code, status, response.text)
        message = {"001": f"{field} is an invalid field on this endpoint",
                   "003": f"Storage failed while handling {field}"}.get(
                       code, f"The value provided for {field} is invalid")
        self.assertEqual(response.json(), {"error_code": code, "error_message": message})

    def test_create_account_requires_admin_key(self):
        body = {"id": 7, "name": "Shop"}
        for headers in ({}, self.bearer("wrong"), self.bearer(self.token)):
            with self.subTest(headers=headers):
                self.assert_error(self.client.post("/accounts", json=body, headers=headers),
                                  401, "002", "access_token")
        # A valid-JSON body of the wrong shape is still rejected by auth first.
        self.assert_error(self.client.post("/accounts", json=[1]), 401, "002", "access_token")
        self.assertEqual(self.client.get("/accounts/7").status_code, 404)
        unconfigured = TestClient(self.api.create_app(admin_key=None))
        self.assert_error(unconfigured.post("/accounts", json=body,
                                            headers=self.bearer("anything")),
                          401, "002", "access_token")

    def test_create_and_get_account(self):
        response = self.client.post("/accounts", json={"id": 7, "name": "Shop"},
                                    headers=self.bearer(ADMIN_KEY))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(set(response.json()), {"id", "access_token"})
        self.assertEqual(response.json()["id"], 7)
        response = self.client.get("/accounts/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": 7, "name": "Shop"})
        for missing in ("8", "abc"):
            with self.subTest(account_id=missing):
                self.assert_error(self.client.get(f"/accounts/{missing}"),
                                  404, "002", "account_id")

    def test_duplicate_and_invalid_accounts(self):
        admin = self.bearer(ADMIN_KEY)
        self.assert_error(self.client.post("/accounts", json={"id": 1, "name": "Again"},
                                           headers=admin), 400, "002", "account_id")
        self.assert_error(self.client.post("/accounts", json={"id": 7, "name": "x", "extra": 1},
                                           headers=admin), 400, "001", "extra")

    def test_rename_account_requires_bearer(self):
        for headers in ({}, self.bearer("wrong"), {"Authorization": "Basic xyz"}):
            with self.subTest(headers=headers):
                self.assert_error(self.client.patch("/accounts/1", json={"name": "New"},
                                                    headers=headers),
                                  401, "002", "access_token")
        response = self.client.patch("/accounts/1", json={"name": "New"},
                                     headers=self.bearer(self.token))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": 1, "name": "New"})
        self.assertEqual(self.client.get("/accounts/1").json(), {"id": 1, "name": "New"})

    def test_malformed_bodies_use_error_shape(self):
        admin = self.bearer(ADMIN_KEY)
        for kwargs in ({"content": b"not json"}, {"json": [1]}, {}):
            with self.subTest(body=kwargs):
                self.assert_error(self.client.post("/accounts", headers=admin, **kwargs),
                                  400, "002", "body")

    def publish_ad(self, **body):
        body.setdefault("text", VALID_TEXT)
        if not {"image", "image_hash", "image_name", "video_id"} & set(body):
            body["image"] = self.seeded[0]["url"]
        return self.client.post("/accounts/1/ads", json=body, headers=self.bearer(self.token))

    def test_publish_and_read_ad(self):
        response = self.publish_ad()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"id": 1})
        response = self.client.get("/ads/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.publish.get_ads(1))
        self.assertEqual(response.json()["image_hash"], self.seeded[0]["hash"])
        response = self.client.get("/ads/1", params={"fields": "text, image_hash"})
        self.assertEqual(response.json(),
                         {"text": VALID_TEXT, "image_hash": self.seeded[0]["hash"]})
        self.assert_error(self.client.get("/ads/1", params={"fields": "nope"}),
                          400, "001", "nope")
        for missing in ("99", "abc"):
            with self.subTest(ad_id=missing):
                self.assert_error(self.client.get(f"/ads/{missing}"), 404, "002", "ad_id")

    def test_publish_rejects_ids_and_tokens_in_body(self):
        self.assert_error(self.publish_ad(creator_id=1), 400, "001", "creator_id")
        self.assert_error(self.publish_ad(access_token=self.token), 400, "001", "access_token")
        body = {"text": VALID_TEXT, "image": self.seeded[0]["url"]}
        self.assert_error(self.client.post("/accounts/1/ads", json=body),
                          401, "002", "access_token")
        self.assert_error(self.client.post("/accounts/99/ads", json=body,
                                           headers=self.bearer(self.token)),
                          400, "002", "creator_id")
        self.assertIsNone(self.publish.get_ads(1))

    def test_list_ads_pages_without_token(self):
        for _ in range(3):
            self.assertEqual(self.publish_ad().status_code, 201)
        first = self.client.get("/accounts/1/ads", params={"limit": 2})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual([ad["id"] for ad in first.json()["data"]], [3, 2])
        self.assertIsNotNone(first.json()["next_cursor"])
        second = self.client.get("/accounts/1/ads",
                                 params={"limit": 2, "cursor": first.json()["next_cursor"]})
        self.assertEqual([ad["id"] for ad in second.json()["data"]], [1])
        self.assertIsNone(second.json()["next_cursor"])
        self.assert_error(self.client.get("/accounts/1/ads", params={"limit": 0}),
                          400, "002", "limit")

    def upload_image(self, content, token=None, **params):
        params.setdefault("filename", "a.png")
        return self.client.post("/accounts/1/images", content=content, params=params,
                                headers=self.bearer(token or self.token))

    def assert_spool_empty(self):
        uploads = self.data_dir / "uploads"
        self.assertEqual(list(uploads.iterdir()) if uploads.exists() else [], [])

    def test_upload_image_and_list(self):
        response = self.upload_image(b"img-bytes", image_name="hero")
        expected = hashlib.sha256(b"img-bytes").hexdigest()[:32]
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"hash": expected})
        self.assert_spool_empty()
        response = self.client.get("/accounts/1/images", headers=self.bearer(self.token))
        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()["data"][0]
        self.assertEqual((image["hash"], image["image_name"]), (expected, "hero"))
        self.assertEqual(self.publish_ad(image_name="hero").status_code, 201)
        self.assertEqual(self.upload_image(b"unnamed").status_code, 201)

    def test_upload_ignores_client_paths(self):
        sample = BASE_DIR / "file_handle_store" / "sample.jpg"
        response = self.upload_image(b"other", filename=str(sample))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"hash": hashlib.sha256(b"other").hexdigest()[:32]})

    def test_upload_rejected_before_spooling(self):
        cases = [
            (self.upload_image(b"x", token="wrong"), 401, "002", "access_token"),
            (self.client.post("/accounts/99/images", content=b"x", params={"filename": "a.png"},
                              headers=self.bearer(self.token)), 400, "002", "owner_id"),
            (self.upload_image(b"x", filename="a.txt"), 400, "002", "file"),
            (self.upload_image(b"x", filename="a.\x00png"), 400, "002", "file"),
            (self.client.post("/accounts/1/images", content=b"x", params={"filename": "a.png"},
                              headers={**self.bearer(self.token), "Content-Length": "abc"}),
             400, "002", "file"),
            (self.upload_image(b"x", image_name="  "), 400, "002", "image_name"),
        ]
        with patch.object(self.image_store, "MAX_IMAGE_BYTES", 4):
            cases.append((self.upload_image(b"12345"), 413, "002", "file"))
        for index, (response, status, code, field) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_error(response, status, code, field)
        self.assert_spool_empty()
        self.assertEqual(self.client.get("/accounts/1/images",
                                         headers=self.bearer(self.token)).json()["data"], [])

    def test_spool_cleanup_on_stream_failure(self):
        async def stream():
            yield b"abc"
            raise ClientDisconnect()

        request = SimpleNamespace(app=self.client.app, headers={"content-length": "10"},
                                  stream=stream)
        with self.assertRaises(ClientDisconnect):
            asyncio.run(self.api.spool_upload(request, "1", self.token, "a.png",
                                              self.validation.IMAGE_EXTENSIONS, 100))
        self.assert_spool_empty()

    def test_upload_storage_failure_is_503(self):
        with patch.object(self.image_store, "store_image", side_effect=RuntimeError("disk full")):
            self.assert_error(self.upload_image(b"img-bytes"), 503, "003", "file")
        self.assert_spool_empty()

    def test_spool_disk_failure_is_503(self):
        with patch.object(self.api.tempfile, "NamedTemporaryFile",
                          side_effect=OSError(28, "No space left on device")):
            self.assert_error(self.upload_image(b"img-bytes"), 503, "003", "file")

        async def stream():
            yield b"abc"
            raise OSError(28, "No space left on device")

        request = SimpleNamespace(app=self.client.app, headers={"content-length": "10"},
                                  stream=stream)
        with self.assertRaises(self.validation.StorageError):
            asyncio.run(self.api.spool_upload(request, "1", self.token, "a.png",
                                              self.validation.IMAGE_EXTENSIONS, 100))
        self.assert_spool_empty()

    def test_list_images_requires_token(self):
        self.assert_error(self.client.get("/accounts/1/images"), 401, "002", "access_token")

    def upload_video(self, content=b"video", token=None, **params):
        params.setdefault("filename", "v.mp4")
        params.setdefault("name", "Promo")
        return self.client.post("/accounts/1/videos", content=content, params=params,
                                headers=self.bearer(token or self.token))

    def test_upload_get_rename_video(self):
        response = self.upload_video()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"id": 1})
        self.assert_spool_empty()
        owner = self.bearer(self.token)
        response = self.client.get("/videos/1", headers=owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["name"], response.json()["owner_id"]), ("Promo", 1))
        self.assertEqual(response.json()["video_hash"], hashlib.sha256(b"video").hexdigest()[:32])
        response = self.client.patch("/videos/1", json={"name": "Promo 2"}, headers=owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "Promo 2")
        response = self.client.get("/accounts/1/videos", headers=owner)
        self.assertEqual([video["name"] for video in response.json()["data"]], ["Promo 2"])
        self.assertIsNone(response.json()["next_cursor"])

    def test_video_not_found_and_wrong_token_match(self):
        self.assertEqual(self.upload_video().status_code, 201)
        other = self.create_account(2, "Advertiser Two")
        responses = [self.client.get("/videos/99", headers=self.bearer(self.token)),
                     self.client.get("/videos/1", headers=self.bearer(other)),
                     self.client.get("/videos/1"),
                     self.client.patch("/videos/1", json={"name": "Taken"},
                                       headers=self.bearer(other))]
        for index, response in enumerate(responses):
            with self.subTest(case=index):
                self.assert_error(response, 404, "002", "video_id")

    def test_video_upload_rejected_before_spooling(self):
        cases = [
            (self.client.post("/accounts/1/videos", content=b"x", params={"filename": "v.mp4"},
                              headers=self.bearer(self.token)), 400, "002", "name"),
            (self.upload_video(filename="v.txt"), 400, "002", "file"),
            (self.upload_video(token="wrong"), 401, "002", "access_token"),
        ]
        with patch.object(self.video_store, "MAX_VIDEO_BYTES", 4):
            cases.append((self.upload_video(b"12345"), 413, "002", "file"))
        for index, (response, status, code, field) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_error(response, status, code, field)
        self.assert_spool_empty()
        self.assertEqual(self.client.get("/accounts/1/videos",
                                         headers=self.bearer(self.token)).json()["data"], [])

    def test_every_http_error_uses_error_shape(self):
        latin1 = self.client.post("/accounts", content=b'{"id": 5, "name": "caf\xe9"}',
                                  headers={**self.bearer(ADMIN_KEY),
                                           "Content-Type": "application/json"})
        self.assert_error(latin1, 400, "002", "body")
        self.assert_error(self.client.get("/nowhere"), 404, "002", "path")
        self.assert_error(self.client.delete("/accounts/1"), 405, "002", "method")

    def test_json_bodies_are_capped_before_auth(self):
        oversized = b"x" * (self.api.MAX_JSON_BYTES + 1)
        for path, method in (("/accounts", "POST"), ("/accounts/1", "PATCH"),
                             ("/accounts/1/ads", "POST"), ("/videos/1", "PATCH")):
            with self.subTest(path=path):
                self.assert_error(self.client.request(method, path, content=oversized),
                                  413, "002", "body")
        # Without a declared length the size is unknown until read, so chunked JSON is refused.
        self.assert_error(self.client.post("/accounts", content=iter([b"{}"]),
                                           headers=self.bearer(ADMIN_KEY)),
                          411, "002", "body")
        # Uploads have their own, larger limits.
        self.assertEqual(self.upload_image(b"x" * (self.api.MAX_JSON_BYTES + 1)).status_code,
                         201)

    def test_storage_failure_is_503(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        repository.get_account = Mock(side_effect=sqlite3.OperationalError("disk I/O error"))
        client = TestClient(self.api.create_app(self.service.AdsService(repository),
                                                admin_key=ADMIN_KEY))
        self.assert_error(client.get("/accounts/1"), 503, "003", "account")


if __name__ == "__main__":
    unittest.main()
