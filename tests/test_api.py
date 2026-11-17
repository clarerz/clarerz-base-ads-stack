"""HTTP API checks through FastAPI's in-process TestClient, on isolated storage."""

import asyncio
import hashlib
import html
import json
import os
import re
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
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
MEDIA_KEY = "test-media"
# A PNG header declaring 2x3 pixels: enough for the size and the sniffed type.
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 2, 3) + b"\x08\x02\x00\x00\x00"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-api-test-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        load_isolated_modules(self, self.data_dir, MODULE_NAMES + ("api",))
        # The environment is restored on cleanup; an exported key must not stand in for None.
        os.environ.pop("ADS_ADMIN_KEY", None)
        os.environ.pop("ADS_MEDIA_KEY", None)
        # Signing and verifying preview URLs read the service clock.
        self.now = 1_800_000_000
        self.client = self.make_client(MEDIA_KEY)
        self.token = self.create_account(1, "Advertiser One")
        self.seeded = json.loads((BASE_DIR / "images.json").read_text())

    def make_client(self, media_key):
        service = self.service.AdsService(
            self.repository.SQLiteAdsRepository(self.publish.DB_PATH), clock=lambda: self.now)
        return TestClient(self.api.create_app(service=service, admin_key=ADMIN_KEY,
                                              media_key=media_key))

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

    def assert_duplicate(self, response, **existing):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {
            "error_code": "004",
            "error_message": "This file was already uploaded to this account", **existing})

    def test_same_account_duplicate_uploads_conflict(self):
        image_hash = self.upload_image(b"img-bytes", image_name="hero").json()["hash"]
        video_id = self.upload_video().json()["id"]
        # The same bytes under another file name, label, or video name name the existing
        # asset and change nothing.
        self.assert_duplicate(self.upload_image(b"img-bytes", filename="copy.gif",
                                                image_name="renamed"), hash=image_hash)
        self.assert_duplicate(self.upload_video(filename="copy.mov", name="Other"), id=video_id)
        self.assert_spool_empty()
        owner = self.bearer(self.token)
        images = self.client.get("/accounts/1/images", headers=owner).json()["data"]
        self.assertEqual([(image["hash"], image["image_name"]) for image in images],
                         [(image_hash, "hero")])
        videos = self.client.get("/accounts/1/videos", headers=owner).json()["data"]
        self.assertEqual([(video["id"], video["name"]) for video in videos], [(video_id, "Promo")])
        # Different bytes are not duplicates, however similar.
        self.assertEqual(self.upload_image(b"img-bytes!").status_code, 201)
        self.assertEqual(self.upload_video(b"video!").status_code, 201)
        # Another account's identical uploads succeed, so the error reveals nothing about
        # other accounts' files.
        other = self.bearer(self.create_account(2, "Advertiser Two"))
        response = self.client.post("/accounts/2/images", content=b"img-bytes",
                                    params={"filename": "a.png"}, headers=other)
        self.assertEqual((response.status_code, response.json()), (201, {"hash": image_hash}))
        response = self.client.post("/accounts/2/videos", content=b"video",
                                    params={"filename": "v.mp4", "name": "Promo"}, headers=other)
        self.assertEqual(response.status_code, 201, response.text)

    def test_duplicate_check_storage_failure_is_503(self):
        self.upload_image(b"img-bytes")
        self.upload_video()
        repository = self.client.app.state.service.repository
        failure = sqlite3.OperationalError("database is locked")
        with patch.object(repository, "owns_image", side_effect=failure):
            self.assert_error(self.upload_image(b"img-bytes"), 503, "003", "file")
        with patch.object(repository, "get_video_by_hash", side_effect=failure):
            self.assert_error(self.upload_video(), 503, "003", "file")
        self.assert_spool_empty()

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

    def lookup(self, token=None, **params):
        return self.client.get("/accounts/1/images/lookup", params=params,
                               headers=self.bearer(token or self.token))

    def assert_media(self, url, content, media_type, headers=None):
        response = self.client.get(url, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, content)
        self.assertEqual(response.headers["content-type"], media_type)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"], "sandbox")
        return response

    def test_image_lookup_returns_signed_preview(self):
        image_hash = self.upload_image(PNG, image_name="hero").json()["hash"]
        expected = {"hash": image_hash, "owner_id": 1, "creation_time": self.now,
                    "image_name": "hero", "image_url": None, "width": 2, "height": 3}
        by_hash = self.lookup(image_hash=image_hash)
        by_name = self.lookup(image_name=" hero ")
        for response in (by_hash, by_name):
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            preview_url = body.pop("preview_url")
            self.assertEqual(body, expected)
            self.assertTrue(preview_url.startswith(
                f"http://testserver/accounts/1/media/images/{image_hash}?exp={self.now + 600}&sig="))
        response = self.assert_media(by_hash.json()["preview_url"], PNG, "image/png")
        self.assertEqual(response.headers["cache-control"], "private, max-age=600, immutable")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        # Range requests work, so large media can stream.
        response = self.client.get(by_hash.json()["preview_url"], headers={"Range": "bytes=0-3"})
        self.assertEqual((response.status_code, response.content), (206, PNG[:4]))
        # The cache lifetime never outlasts the signature.
        self.now += 590
        response = self.client.get(by_hash.json()["preview_url"])
        self.assertEqual(response.headers["cache-control"], "private, max-age=10, immutable")

    def test_image_lookup_errors(self):
        other_token = self.create_account(2, "Advertiser Two")
        theirs = self.client.post("/accounts/2/images", content=b"theirs",
                                  params={"filename": "a.png", "image_name": "theirs"},
                                  headers=self.bearer(other_token)).json()["hash"]
        # Missing images, other accounts' images, and malformed lookups are all 404.
        for params, field in (({"image_hash": theirs}, "image_hash"),
                              ({"image_hash": "f" * 32}, "image_hash"),
                              ({"image_hash": "not-a-hash"}, "image_hash"),
                              ({"image_name": "theirs"}, "image_name"), ({}, "image_hash")):
            with self.subTest(params=params):
                self.assert_error(self.lookup(**params), 404, "002", field)
        self.assert_error(self.lookup(token="wrong", image_hash=theirs), 401, "002",
                          "access_token")
        self.assert_error(self.client.get("/accounts/9/images/lookup",
                                          params={"image_hash": theirs},
                                          headers=self.bearer(self.token)),
                          400, "002", "owner_id")

    def test_listings_and_videos_carry_preview_urls(self):
        link = self.seeded[1]
        self.assertEqual(self.publish_ad(image=link["url"]).status_code, 201)
        uploaded = self.upload_image(b"not an image").json()["hash"]
        owner = self.bearer(self.token)
        images = {image["hash"]: image["preview_url"] for image in
                  self.client.get("/accounts/1/images", headers=owner).json()["data"]}
        # A link-ingested image has no stored bytes; its source link is the preview.
        self.assertEqual(images[link["hash"]], link["url"])
        # Bytes with no recognized media header are served as an opaque download.
        self.assert_media(images[uploaded], b"not an image", "application/octet-stream")
        self.assertEqual(self.upload_video(MP4).status_code, 201)
        video_hash = hashlib.sha256(MP4).hexdigest()[:32]
        responses = (self.client.get("/videos/1", headers=owner).json(),
                     self.client.patch("/videos/1", json={"name": "Promo 2"}, headers=owner).json(),
                     self.client.get("/accounts/1/videos", headers=owner).json()["data"][0])
        for video in responses:
            with self.subTest(video=video):
                self.assertTrue(video["preview_url"].startswith(
                    f"http://testserver/accounts/1/media/videos/{video_hash}?exp="))
                self.assert_media(video["preview_url"], MP4, "video/mp4")

    def test_media_types_are_sniffed(self):
        cases = {"b.gif": (b"GIF89a" + b"\x00" * 8, "image/gif"),
                 "c.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
                 "d.webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp")}
        for filename, (content, media_type) in cases.items():
            with self.subTest(filename=filename):
                image_hash = self.upload_image(content, filename=filename).json()["hash"]
                self.assert_media(self.lookup(image_hash=image_hash).json()["preview_url"],
                                  content, media_type)
        videos = {"a.mov": (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8, "video/quicktime"),
                  "b.webm": (b"\x1a\x45\xdf\xa3\x00\x00webm", "video/webm"),
                  "c.mkv": (b"\x1a\x45\xdf\xa3\x00\x00matroska", "video/x-matroska"),
                  "d.avi": (b"RIFF\x00\x00\x00\x00AVI LIST", "video/x-msvideo")}
        for video_id, (filename, (content, media_type)) in enumerate(videos.items(), 1):
            with self.subTest(filename=filename):
                self.assertEqual(self.upload_video(content, filename=filename).status_code, 201)
                url = self.client.get(f"/videos/{video_id}",
                                      headers=self.bearer(self.token)).json()["preview_url"]
                self.assert_media(url, content, media_type)

    def test_media_urls_reject_tampering_and_expiry(self):
        image_hash = self.upload_image(PNG).json()["hash"]
        other_hash = self.upload_image(b"other").json()["hash"]
        # The same bytes stored as a video too, so a swapped kind would find a file.
        self.assertEqual(self.upload_video(PNG, filename="v.mp4").status_code, 201)
        url = self.lookup(image_hash=image_hash).json()["preview_url"]
        path, _, query = url.removeprefix("http://testserver").partition("?")
        params = dict(pair.split("=") for pair in query.split("&"))
        token_hash = self.validation.hash_token(self.token)
        stale = self.api.media_signature(MEDIA_KEY, 1, "images", image_hash, params["exp"],
                                         self.validation.hash_token("old token"))
        signature = lambda kind, media_hash, exp, account_id=1, key=MEDIA_KEY: (
            self.api.media_signature(key, account_id, kind, media_hash, exp, token_hash))
        bad = [
            path,  # No signature.
            f"{path}?exp={params['exp']}",
            f"{path}?exp={params['exp']}&sig={'0' * 64}",
            f"{path}?exp={params['exp']}&sig={params['sig'][:-1]}",
            f"{path}?exp={params['exp']}&sig=\u00e9",
            f"{path}?exp={int(params['exp']) + 1}&sig={params['sig']}",  # Extended expiry.
            f"{path}?exp=abc&sig={params['sig']}",
            f"/accounts/1/media/images/{other_hash}?{query}",  # Reused for other content.
            f"/accounts/1/media/videos/{image_hash}?{query}",  # ... or another kind.
            f"/accounts/2/media/images/{image_hash}?{query}",  # ... or another account.
            f"/accounts/1/media/other/{image_hash}?exp={params['exp']}"
            f"&sig={signature('other', image_hash, params['exp'])}",
            # Correctly signed, but nothing is stored under the hash.
            f"/accounts/1/media/images/{'f' * 32}?exp={params['exp']}"
            f"&sig={signature('images', 'f' * 32, params['exp'])}",
            # Signed with another key, or without the account's current token.
            f"{path}?exp={params['exp']}"
            f"&sig={signature('images', image_hash, params['exp'], key='other-key')}",
            f"{path}?exp={params['exp']}&sig={stale}",
        ]
        for candidate in bad:
            with self.subTest(url=candidate):
                self.assert_error(self.client.get(candidate), 404, "002", "media")
        self.now += 599
        self.assert_media(url, PNG, "image/png")
        self.now += 1  # Expired at exactly exp.
        self.assert_error(self.client.get(url), 404, "002", "media")

    def test_media_is_served_only_to_the_owning_account(self):
        other_token = self.create_account(2, "Advertiser Two")
        image_hash = self.upload_image(PNG).json()["hash"]
        theirs = self.client.post("/accounts/2/images", content=b"theirs",
                                  params={"filename": "t.png"},
                                  headers=self.bearer(other_token)).json()["hash"]
        self.assertEqual(self.upload_video(MP4).status_code, 201)
        video_hash = hashlib.sha256(MP4).hexdigest()[:32]
        mine = f"/accounts/1/media/images/{image_hash}"
        # The account's own token works without a signature, for server-side clients.
        response = self.client.get(mine, headers=self.bearer(self.token))
        self.assertEqual((response.status_code, response.content), (200, PNG))
        self.assert_media(f"/accounts/1/media/videos/{video_hash}", MP4, "video/mp4",
                          headers=self.bearer(self.token))
        # Another account's token, or a wrong one, is rejected like on every account route.
        for token in (other_token, "wrong"):
            with self.subTest(token=token):
                self.assert_error(self.client.get(mine, headers=self.bearer(token)), 401, "002",
                                  "access_token")
        # Media the account does not own is missing, even though the bytes are stored.
        for path in (f"/accounts/1/media/images/{theirs}",
                     f"/accounts/1/media/videos/{image_hash}",
                     f"/accounts/1/media/images/{'f' * 32}"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path, headers=self.bearer(self.token)), 404,
                                  "002", "media")
        # Rotating the token revokes every preview URL issued under the old one.
        url = self.lookup(image_hash=image_hash).json()["preview_url"]
        self.assert_media(url, PNG, "image/png")
        new_token = self.client.app.state.service.issue_access_token(1)
        self.assert_error(self.client.get(url), 404, "002", "media")
        url = self.lookup(token=new_token, image_hash=image_hash).json()["preview_url"]
        self.assert_media(url, PNG, "image/png")

    def test_no_media_key_means_no_previews(self):
        image_hash = self.upload_image(PNG).json()["hash"]
        url = self.lookup(image_hash=image_hash).json()["preview_url"]
        self.client = self.make_client(None)
        self.assertIsNone(self.lookup(image_hash=image_hash).json()["preview_url"])
        self.assert_error(self.client.get(url), 404, "002", "media")
        # An empty key is the same as none.
        self.client = self.make_client("")
        self.assertIsNone(self.lookup(image_hash=image_hash).json()["preview_url"])
        self.assert_error(self.client.get(url), 404, "002", "media")
        # The account's token still works.
        self.assert_media(f"/accounts/1/media/images/{image_hash}", PNG, "image/png",
                          headers=self.bearer(self.token))

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

    def previews(self, ad_id=1, token=None, account_id=1, **params):
        return self.client.get(f"/accounts/{account_id}/ads/{ad_id}/previews", params=params,
                               headers=self.bearer(token or self.token))

    def preview_urls(self, ad_id=1, token=None):
        response = self.previews(ad_id, token)
        self.assertEqual(response.status_code, 200, response.text)
        return {entry["format"]: entry["preview_url"] for entry in response.json()["data"]}

    def preview_page(self, url, headers=None):
        response = self.client.get(url, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        # No scripts, an opaque origin, and media only from this server or the image's link.
        self.assertEqual(response.headers["content-security-policy"],
                         "sandbox; default-src 'none'; img-src http: https:; "
                         "media-src http://testserver; style-src 'unsafe-inline'")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.text

    @staticmethod
    def media_sources(page):
        return [src.replace("&amp;", "&") for src in re.findall(r'src="([^"]*)"', page)]

    def test_ad_previews_list_every_format(self):
        square, landscape = self.seeded[0], self.seeded[1]  # 1080x1080 and 1200x628.
        self.assertEqual(self.publish_ad(image=square["url"]).status_code, 201)
        response = self.previews()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        urls = [entry.pop("preview_url") for entry in data]
        # VALID_TEXT is longer than a banner's 40 characters.
        self.assertEqual(data, [
            {"format": "feed", "width": 500, "height": 640, "warnings": []},
            {"format": "story", "width": 360, "height": 640, "warnings": ["media_cropped"]},
            {"format": "banner", "width": 600, "height": 354,
             "warnings": ["text_truncated", "media_cropped"]},
        ])
        for url, name in zip(urls, ("feed", "story", "banner")):
            self.assertTrue(url.startswith(f"http://testserver/accounts/1/ads/1/previews/{name}"
                                           f"?exp={self.now + 600}&sig="), url)
        response = self.previews(ad_format="banner")
        self.assertEqual([entry["format"] for entry in response.json()["data"]], ["banner"])
        long_text = "Fresh picks " * 11
        self.assertEqual(self.publish_ad(image=landscape["url"], text=long_text).status_code, 201)
        warnings = {entry["format"]: entry["warnings"] for entry in self.previews(2).json()["data"]}
        self.assertEqual(warnings, {"feed": ["text_truncated", "media_cropped"],
                                    "story": ["text_truncated", "media_cropped"],
                                    "banner": ["text_truncated"]})
        # A video's size is unknown, so nothing is reported as cropped.
        self.assertEqual(self.upload_video(MP4).status_code, 201)
        self.assertEqual(self.publish_ad(video_id=1, text="Watch now").status_code, 201)
        warnings = [entry["warnings"] for entry in self.previews(3).json()["data"]]
        self.assertEqual(warnings, [[], [], []])

    def test_preview_pages_render_each_format(self):
        link = self.seeded[0]
        self.assertEqual(self.publish_ad(image=link["url"]).status_code, 201)
        page = self.preview_page(self.preview_urls()["feed"])
        self.assertIn("<strong>Advertiser One</strong>", page)
        self.assertIn("Sponsored", page)
        self.assertIn(f"<p class=\"text\">{VALID_TEXT}</p>", page)
        # A link image is shown from its link.
        self.assertIn(f'<img class="media" src="{link["url"]}" alt="Ad image">', page)
        # Uploaded bytes are shown through a signed media URL.
        image_hash = self.upload_image(PNG).json()["hash"]
        self.assertEqual(self.publish_ad(image_hash=image_hash).status_code, 201)
        for name, url in self.preview_urls(2).items():
            with self.subTest(format=name):
                [src] = self.media_sources(self.preview_page(url))
                self.assertTrue(src.startswith(
                    f"http://testserver/accounts/1/media/images/{image_hash}?exp="), src)
                self.assert_media(src, PNG, "image/png")
        # Video plays muted in every format; only the story autoplays.
        self.assertEqual(self.upload_video(MP4).status_code, 201)
        self.assertEqual(self.publish_ad(video_id=1).status_code, 201)
        pages = {name: self.preview_page(url) for name, url in self.preview_urls(3).items()}
        self.assertIn("autoplay muted loop playsinline", pages["story"])
        for name in ("feed", "banner"):
            self.assertIn("controls muted playsinline", pages[name])
            self.assertNotIn("autoplay", pages[name])
        for page in pages.values():
            [src] = self.media_sources(page)
            self.assert_media(src, MP4, "video/mp4")
        # Bytes no browser can show render a labeled empty box.
        placeholder = self.upload_image(b"not an image").json()["hash"]
        self.assertEqual(self.publish_ad(image_hash=placeholder).status_code, 201)
        page = self.preview_page(self.preview_urls(4)["feed"])
        self.assertEqual(self.media_sources(page), [])
        self.assertIn('role="img" aria-label="Media unavailable"', page)
        # Long text is cut per format; the feed offers "See more".
        long_text = "Fresh picks " * 11
        self.assertEqual(self.publish_ad(text=long_text).status_code, 201)
        urls = self.preview_urls(5)
        self.assertIn(f"{long_text[:125].rstrip()}… <span class=\"more\">See more</span>",
                      self.preview_page(urls["feed"]))
        banner = self.preview_page(urls["banner"])
        self.assertIn(f"{long_text[:40].rstrip()}…</p>", banner)
        self.assertNotIn("See more", banner)

    def test_preview_page_escapes_advertiser_text(self):
        rename = self.client.patch("/accounts/1", json={"name": "<i>One</i>"},
                                   headers=self.bearer(self.token))
        self.assertEqual(rename.status_code, 200, rename.text)
        text = '<img src=x onerror=alert(1)> & "deal"'
        self.assertEqual(self.publish_ad(text=text).status_code, 201)
        page = self.preview_page(self.preview_urls()["feed"])
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<i>One</i>", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; &amp; &quot;deal&quot;", page)
        self.assertIn("&lt;i&gt;One&lt;/i&gt;", page)
        # Links may contain quotes; they cannot leave the src attribute.
        link = 'https://cdn.example.com/a"onerror="alert(1)'
        self.assertEqual(self.publish_ad(image=link).status_code, 201)
        page = self.preview_page(self.preview_urls(2)["feed"])
        self.assertIn('src="https://cdn.example.com/a&quot;onerror=&quot;alert(1)"', page)

    def test_preview_never_links_media_the_account_does_not_own(self):
        # An ad saved before ownership was recorded, naming bytes another account uploaded.
        other_token = self.create_account(2, "Advertiser Two")
        image_hash = self.client.post("/accounts/2/images", content=PNG,
                                      params={"filename": "a.png"},
                                      headers=self.bearer(other_token)).json()["hash"]
        draft = self.models.AdDraft(1, VALID_TEXT, image_hash=image_hash)
        self.client.app.state.service.repository.save_ad(draft, self.now)
        page = self.preview_page(self.preview_urls()["feed"])
        self.assertEqual(self.media_sources(page), [])
        self.assertIn('aria-label="Media unavailable"', page)

    def test_ad_previews_errors(self):
        other_token = self.create_account(2, "Advertiser Two")
        self.assertEqual(self.publish_ad().status_code, 201)
        theirs = self.client.post("/accounts/2/ads", headers=self.bearer(other_token),
                                  json={"text": VALID_TEXT, "image": self.seeded[0]["url"]})
        self.assertEqual(theirs.json(), {"id": 2})
        self.assert_error(self.client.get("/accounts/1/ads/1/previews"), 401, "002",
                          "access_token")
        # Only a bearer token authenticates; another scheme carrying the token does not.
        for scheme in ("Basic", "Token"):
            with self.subTest(scheme=scheme):
                self.assert_error(self.client.get("/accounts/1/ads/1/previews", headers={
                    "Authorization": f"{scheme} {self.token}"}), 401, "002", "access_token")
        for token in ("wrong", other_token):
            with self.subTest(token=token):
                self.assert_error(self.previews(token=token), 401, "002", "access_token")
        self.assert_error(self.previews(account_id=9), 400, "002", "creator_id")
        # Another account's ad is as missing as no ad.
        for ad_id in (2, 99, "abc"):
            with self.subTest(ad_id=ad_id):
                self.assert_error(self.previews(ad_id), 404, "002", "ad_id")
        self.assert_error(self.previews(ad_format="carousel"), 400, "002", "ad_format")

    def test_previews_need_an_issued_token(self):
        # An account created before authentication has no token, so nothing authenticates.
        self.assertEqual(self.publish_ad().status_code, 201)
        with sqlite3.connect(self.publish.DB_PATH) as conn:
            conn.execute("UPDATE accounts SET token_hash = NULL WHERE id = 1")
        self.assert_error(self.previews(), 401, "002", "access_token")
        self.assert_error(self.client.get("/accounts/1/ads/1/previews/feed",
                                          headers=self.bearer(self.token)),
                          401, "002", "access_token")
        # A URL signed over the missing token hash opens nothing either.
        exp = self.now + 600
        sig = self.api.media_signature(MEDIA_KEY, 1, "preview:feed", "1", exp, None)
        self.assert_error(self.client.get(f"/accounts/1/ads/1/previews/feed?exp={exp}&sig={sig}"),
                          404, "002", "preview")
        new_token = self.client.app.state.service.issue_access_token(1)
        self.preview_page(self.preview_urls(token=new_token)["feed"])

    def test_preview_url_grants_nothing_past_its_expiry(self):
        image_hash = self.upload_image(PNG).json()["hash"]
        self.assertEqual(self.publish_ad(image_hash=image_hash).status_code, 201)
        url = self.preview_urls()["feed"]
        expires = self.now + 600
        # Opened just before it expires, the page's media links expire with it.
        self.now += 590
        [src] = self.media_sources(self.preview_page(url))
        self.assertIn(f"?exp={expires}&sig=", src)
        self.assert_media(src, PNG, "image/png")
        self.now += 10
        self.assert_error(self.client.get(url), 404, "002", "preview")
        self.assert_error(self.client.get(src), 404, "002", "media")
        # A preview signature never opens media, even for content the account owns.
        query = url.partition("?")[2]
        self.assert_error(self.client.get(f"/accounts/1/media/images/{image_hash}?{query}"),
                          404, "002", "media")

    def test_preview_urls_reject_tampering_and_expiry(self):
        other_token = self.create_account(2, "Advertiser Two")
        for _ in range(2):
            self.assertEqual(self.publish_ad().status_code, 201)
        theirs = self.client.post("/accounts/2/ads", headers=self.bearer(other_token),
                                  json={"text": VALID_TEXT, "image": self.seeded[0]["url"]})
        self.assertEqual(theirs.json(), {"id": 3})
        url = self.preview_urls()["feed"]
        path, _, query = url.removeprefix("http://testserver").partition("?")
        params = dict(pair.split("=") for pair in query.split("&"))
        exp = params["exp"]
        sign = lambda kind, subject: self.api.media_signature(
            MEDIA_KEY, 1, kind, subject, exp, self.validation.hash_token(self.token))
        bad = [
            path,  # No signature.
            f"{path}?exp={exp}",
            f"{path}?exp={exp}&sig={'0' * 64}",
            f"{path}?exp={int(exp) + 1}&sig={params['sig']}",  # Extended expiry.
            f"/accounts/1/ads/1/previews/story?{query}",  # Reused for another format,
            f"/accounts/1/ads/2/previews/feed?{query}",  # ... another ad,
            f"/accounts/2/ads/1/previews/feed?{query}",  # ... or another account.
            f"{path}?exp={exp}&sig={sign('images', '1')}",  # A media signature.
            # Correctly signed, but the ad is missing, another account's, or the format unknown.
            f"/accounts/1/ads/99/previews/feed?exp={exp}&sig={sign('preview:feed', '99')}",
            f"/accounts/1/ads/3/previews/feed?exp={exp}&sig={sign('preview:feed', '3')}",
            f"/accounts/1/ads/1/previews/carousel?exp={exp}&sig={sign('preview:carousel', '1')}",
        ]
        for candidate in bad:
            with self.subTest(url=candidate):
                self.assert_error(self.client.get(candidate), 404, "002", "preview")
        self.now += 599
        self.preview_page(url)
        self.now += 1  # Expired at exactly exp.
        self.assert_error(self.client.get(url), 404, "002", "preview")
        # Reissuing the token revokes every preview URL issued under the old one.
        url = self.preview_urls()["feed"]
        new_token = self.client.app.state.service.issue_access_token(1)
        self.assert_error(self.client.get(url), 404, "002", "preview")
        self.preview_page(self.preview_urls(token=new_token)["feed"])
        # The account's token works without a signature; a wrong one is a 401.
        self.preview_page(path, headers=self.bearer(new_token))
        for token in ("wrong", other_token):
            with self.subTest(token=token):
                self.assert_error(self.client.get(path, headers=self.bearer(token)), 401, "002",
                                  "access_token")
        self.assert_error(self.client.get("/accounts/1/ads/3/previews/feed",
                                          headers=self.bearer(new_token)), 404, "002", "preview")

    def test_no_media_key_means_no_preview_urls(self):
        image_hash = self.upload_image(PNG).json()["hash"]
        self.assertEqual(self.publish_ad(image_hash=image_hash).status_code, 201)
        url = self.preview_urls()["feed"]
        self.client = self.make_client(None)
        self.assertEqual(set(self.preview_urls().values()), {None})
        self.assert_error(self.client.get(url), 404, "002", "preview")
        # The token still opens the page, but its stored media cannot be linked.
        page = self.preview_page("/accounts/1/ads/1/previews/feed",
                                 headers=self.bearer(self.token))
        self.assertEqual(self.media_sources(page), [])
        self.assertIn('aria-label="Media unavailable"', page)

    def test_preview_storage_failure_is_503(self):
        self.assertEqual(self.publish_ad().status_code, 201)
        url = self.preview_urls()["feed"]
        service = self.client.app.state.service
        with patch.object(service.repository, "get_ad",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assert_error(self.previews(), 503, "003", "ad")
            self.assert_error(self.client.get(url), 503, "003", "preview")

    SHARE_HEADERS = {
        "content-security-policy": "sandbox; default-src 'none'; frame-src http://testserver; "
                                   "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "cache-control": "no-store",
    }

    def share_url(self, ad_id=1):
        response = self.previews(ad_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["share_url"]

    def share(self, url, headers=None):
        """GET a share page; every response, found or not, carries the share page headers."""
        response = self.client.get(url, headers=headers)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        for name, value in self.SHARE_HEADERS.items():
            self.assertEqual(response.headers[name], value, name)
        return response

    @staticmethod
    def frames(page):
        return [(html.unescape(src), int(width), int(height)) for src, width, height in re.findall(
            r'<iframe src="([^"]*)" width="(\d+)" height="(\d+)"', page)]

    def test_previews_listing_returns_a_share_link(self):
        self.assertEqual(self.publish_ad().status_code, 201)
        expires = self.now + 604800
        for params in ({}, {"ad_format": "banner"}):
            with self.subTest(params=params):
                body = self.previews(**params).json()
                self.assertEqual(body["share_expires"], expires)
                self.assertTrue(body["share_url"].startswith(
                    f"http://testserver/accounts/1/ads/1/share?exp={expires}&sig="))
        self.client = self.make_client(None)
        body = self.previews().json()
        self.assertEqual((body["share_url"], body["share_expires"]), (None, None))

    def test_share_page_shows_every_placement(self):
        self.assertEqual(self.publish_ad(text=("Fresh picks " * 11)[:130].rstrip()).status_code, 201)
        self.assertEqual(self.publish_ad(text=("Fresh picks " * 5)[:60].rstrip()).status_code, 201)
        expires = self.now + 604800
        response = self.share(self.share_url())
        self.assertEqual(response.status_code, 200, response.text)
        page = response.text
        frames = self.frames(page)
        self.assertEqual([(width, height) for _, width, height in frames],
                         [(500, 640), (360, 640), (600, 354)])
        for (src, _, _), name in zip(frames, ("feed", "story", "banner")):
            self.assertIn(f"/previews/{name}?exp={expires}", src)
            self.preview_page(src)
        self.assertEqual(page.count("Text is cut off"), 3)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)
        self.assertIn("iframe{display:block;max-width:100%", page)
        self.assertIn("Advertiser One", page)
        self.assertIn("Ad #1", page)
        self.assertIn("Link expires " + time.strftime("%Y-%m-%d %H:%M", time.gmtime(expires))
                      + " UTC", page)
        self.assertEqual(self.share(self.share_url(2)).text.count("Text is cut off"), 1)

    def test_nothing_outlives_the_share_link(self):
        image_hash = self.upload_image(PNG).json()["hash"]
        self.assertEqual(self.publish_ad(image_hash=image_hash).status_code, 201)
        self.assertEqual(self.upload_video(MP4).status_code, 201)
        self.assertEqual(self.publish_ad(video_id=1).status_code, 201)
        expires = self.now + 604800

        def collect():
            urls = [self.share_url(ad_id) for ad_id in (1, 2)]
            frames = [src for url in urls for src, _, _ in self.frames(self.share(url).text)]
            media = [src for frame in frames for src in self.media_sources(self.preview_page(frame))]
            return urls, frames, media

        urls, frames, media = collect()
        self.assertEqual(len(media), 6)
        for url in frames + media:
            self.assertIn(f"exp={expires}", url)
        for url in media:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "private, max-age=600, immutable")
        start = self.now
        self.now = start + 604800
        for url in urls:
            self.assertEqual(self.share(url).status_code, 404)
        for url in frames + media:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.now = start
        urls, frames, media = collect()
        self.client.app.state.service.issue_access_token(1)
        for url in urls:
            self.assertEqual(self.share(url).status_code, 404)
        for url in frames + media:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_share_links_reveal_nothing(self):
        other_token = self.create_account(2, "Advertiser Two")
        self.assertEqual(self.publish_ad().status_code, 201)
        self.assertEqual(self.publish_ad().status_code, 201)
        theirs = self.client.post("/accounts/2/ads", headers=self.bearer(other_token),
                                  json={"text": VALID_TEXT, "image": self.seeded[0]["url"]})
        self.assertEqual(theirs.json(), {"id": 3})
        url = self.share_url()
        path, _, query = url.removeprefix("http://testserver").partition("?")
        params = dict(pair.split("=") for pair in query.split("&"))
        exp, sig = params["exp"], params["sig"]
        token_hash = self.validation.hash_token(self.token)
        sign = lambda scope, subject, expires=exp: self.api.media_signature(
            MEDIA_KEY, 1, scope, subject, expires, token_hash)
        bad = [
            f"{path}?exp={exp}&sig={sig[:-1]}{'0' if sig[-1] != '0' else '1'}",
            f"{path}?exp={int(exp) + 1}&sig={sig}",
            f"{path}?exp=abc&sig={sig}",
            f"{path}?exp=-1&sig={sig}",
            f"{path}?exp=99999999999999999999&sig={sig}",
            path,
            f"/accounts/1/ads/2/share?{query}",
            f"/accounts/2/ads/1/share?{query}",
            f"{path}?exp={exp}&sig={sign('preview:feed', '1')}",
            f"{path}?exp={exp}&sig={sign('images', '1')}",
            f"/accounts/1/ads/99/share?exp={exp}&sig={sign('share', '99')}",
            # Account 2's ad under account 1, signed for account 1.
            f"/accounts/1/ads/3/share?exp={exp}&sig={sign('share', '3')}",
            # Signed with a different media key.
            f"{path}?exp={exp}&sig={self.api.media_signature('other-key', 1, 'share', '1', exp, token_hash)}",
        ]
        for candidate in bad:
            with self.subTest(url=candidate):
                response = self.share(candidate)
                self.assertEqual((response.status_code, response.text),
                                 (404, self.api.previews.SHARE_NOT_FOUND))
        self.assertEqual(self.share(url).status_code, 200)
        self.now += 604800
        response = self.share(url)
        self.assertEqual((response.status_code, response.text),
                         (404, self.api.previews.SHARE_NOT_FOUND))
        self.now -= 604800
        # A share signature opens neither a preview page nor media.
        self.assert_error(self.client.get(f"/accounts/1/ads/1/previews/feed?{query}"),
                          404, "002", "preview")
        image_hash = self.upload_image(PNG).json()["hash"]
        self.assert_error(self.client.get(f"/accounts/1/media/images/{image_hash}?{query}"),
                          404, "002", "media")
        self.assertEqual(self.share(url).status_code, 200)
        self.token = self.client.app.state.service.issue_access_token(1)
        response = self.share(url)
        self.assertEqual((response.status_code, response.text),
                         (404, self.api.previews.SHARE_NOT_FOUND))
        # The account's own token opens the page, with 10-minute frame URLs.
        response = self.share(path, self.bearer(self.token))
        self.assertEqual(response.status_code, 200, response.text)
        frames = self.frames(response.text)
        self.assertEqual(len(frames), 3)
        for src, _, _ in frames:
            self.assertIn(f"exp={self.now + 600}", src)
        # A server without a media key reveals nothing, with or without a token.
        self.client = self.make_client(None)
        for headers in (None, self.bearer(self.token)):
            with self.subTest(keyless=headers):
                response = self.share(url, headers)
                self.assertEqual((response.status_code, response.text),
                                 (404, self.api.previews.SHARE_NOT_FOUND))

    def test_share_page_is_static_and_escaped(self):
        rename = self.client.patch("/accounts/1", json={"name": "<script>x</script>"},
                                   headers=self.bearer(self.token))
        self.assertEqual(rename.status_code, 200, rename.text)
        self.assertEqual(self.publish_ad().status_code, 201)
        page = self.share(self.share_url()).text
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)
        self.assertNotIn("<script", page)

    def test_share_storage_failure_is_503(self):
        self.assertEqual(self.publish_ad().status_code, 201)
        url = self.share_url()
        service = self.client.app.state.service
        with patch.object(service.repository, "get_ad",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assert_error(self.client.get(url), 503, "003", "share")


if __name__ == "__main__":
    unittest.main()
