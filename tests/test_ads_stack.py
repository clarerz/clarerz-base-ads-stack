"""Business behavior checks adapted from simple-ads-stack/tests/test_outputs.py.

Use the canonical API and isolated temporary storage instead of API discovery
and the benchmark worker. Persistence is also checked in a fresh Python process.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


BASE_DIR = Path(__file__).resolve().parents[1]
VALID_TEXT = "Summer sale! Get 50% off today only - limited time offer"
NEW_URL = "https://cdn.example.com/ads/generated_zzz_9187.png"


class AdsStackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-stack-test-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        env = patch.dict(os.environ, {"ADS_STACK_DATA_DIR": str(self.data_dir)})
        env.start()
        self.addCleanup(env.stop)
        modules = patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        # Load fresh modules after setting storage, without creating project data.
        for name in ("models", "repository", "validation", "file_store", "image_store",
                     "video_store", "publishing", "service", "publish"):
            spec = importlib.util.spec_from_file_location(name, BASE_DIR / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            setattr(self, name, module)
        self.seeded = json.loads((BASE_DIR / "images.json").read_text())
        self.token = self.create_account(1, "Advertiser One")

    def create_account(self, account_id, name):
        created = self.publish.create_account(account_id, name)
        self.assertEqual(set(created), {"id", "access_token"}, created)
        return created["access_token"]

    def payload(self, **changes):
        data = {"creator_id": 1, "access_token": self.token, "text": VALID_TEXT,
                "image": self.seeded[0]["url"]}
        data.update(changes)
        return data

    def publish_ok(self, data=None):
        response = self.publish.publish_ad(self.payload() if data is None else data)
        self.assertIn("id", response, response)
        self.assertNotIn("error_code", response)
        return response["id"]

    def assert_error(self, response, code, field):
        self.assertIsInstance(response, dict)
        self.assertEqual(set(response), {"error_code", "error_message"})
        self.assertEqual(response["error_code"], code)
        message = (f"{field} is an invalid field on this endpoint" if code == "001"
                   else f"The value provided for {field} is invalid")
        self.assertEqual(response["error_message"], message)

    def fresh_process(self, code):
        env = dict(os.environ, PYTHONPATH=str(BASE_DIR), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, cwd=self.data_dir,
            capture_output=True, text=True, timeout=15, check=True,
        )
        return json.loads(result.stdout)

    def test_account_persistence_both_fields(self):
        self.assertEqual(self.publish.get_account(1), {"id": 1, "name": "Advertiser One"})

    def test_account_read_returns_none_for_invalid(self):
        for account_id in (999999999, "invalid", None, True):
            with self.subTest(account_id=account_id):
                self.assertIsNone(self.publish.get_account(account_id))

    def test_published_image_ad_record(self):
        before = int(time.time())
        ids = [self.publish_ok(), self.publish_ok()]
        self.assertNotEqual(*ids)
        for ad_id in ids:
            self.assertIs(type(ad_id), int)
            ad = self.publish.get_ad(ad_id)
            self.assertLessEqual(before, ad["creation_time"])
            self.assertLessEqual(ad["creation_time"], int(time.time()))
            # The link is stored as its image hash; the URL itself is not kept.
            self.assertEqual(ad, {"id": ad_id, "system_name": f"ad_{ad_id}",
                                  "image_hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "text": VALID_TEXT,
                                  "creator_id": 1, "creation_time": ad["creation_time"],
                                  "video_id": None})

    def test_image_field_accepts_links_only(self):
        self.publish_ok(self.payload(image=self.seeded[0]["url"]))
        # Local files are uploaded first, then published by hash or name.
        sample = str(BASE_DIR / "file_handle_store/sample.jpg")
        for image in ("sample.jpg", sample, f"file://{sample}", "ftp://cdn.example.com/a.jpg",
                      "not a url", "https://example.com/a b.jpg", "", None, 123):
            with self.subTest(image=image):
                self.assert_error(self.publish.publish_ad(self.payload(image=image)),
                                  "002", "image")
        self.assertEqual(self.stored_image_files(), [])

    def test_publish_requires_valid_creator(self):
        for creator in (999999999, None, True, "invalid"):
            with self.subTest(creator=creator):
                self.assert_error(self.publish.publish_ad(self.payload(creator_id=creator)),
                                  "002", "creator_id")
        data = self.payload()
        del data["creator_id"]
        self.assert_error(self.publish.publish_ad(data), "002", "creator_id")

    def test_persistence_survives_restart(self):
        ad_id = self.publish_ok()
        result = self.fresh_process(
            "import json, publish; print(json.dumps({"
            f"'account': publish.get_account(1), 'ad': publish.get_ads({ad_id})" + "}))"
        )
        self.assertEqual(result["account"], self.publish.get_account(1))
        self.assertEqual(result["ad"], self.publish.get_ads(ad_id))

    def test_seeded_image_store_available(self):
        self.assertEqual(self.image_store.list_images(), self.seeded)
        for image in self.seeded:
            self.assertEqual(self.image_store.get_image_by_url(image["url"]), image)
            self.assertEqual(self.image_store.get_image_by_hash(image["hash"]), image)

    def test_invalid_text_value_002(self):
        for text in ("", "  ", "ab", None, 123, self.seeded[0]["url"], self.seeded[0]["hash"]):
            with self.subTest(text=text):
                self.assert_error(self.publish.publish_ad(self.payload(text=text)),
                                  "002", "text")

    def test_read_returns_none_for_invalid_id(self):
        for ad_id in (999888777, "invalid", None, True):
            with self.subTest(ad_id=ad_id):
                self.assertIsNone(self.publish.get_ads(ad_id))
                self.assertIsNone(self.publish.get_ad(ad_id))

    def test_url_ingestion_records_metadata_once(self):
        self.assertIsNone(self.image_store.get_image_by_url(NEW_URL))
        expected = hashlib.sha256(NEW_URL.encode()).hexdigest()[:32]
        ads = [self.publish.get_ad(self.publish_ok(self.payload(image=NEW_URL))) for _ in range(2)]
        self.assertEqual([ad["image_hash"] for ad in ads], [expected, expected])
        stored = self.fresh_process(
            f"import json, image_store; print(json.dumps(image_store.get_image_by_url({NEW_URL!r})))"
        )
        self.assertEqual(stored["hash"], expected)
        # URL ingestion is metadata-only: nothing is downloaded or copied, and it is recorded once.
        self.assertNotIn("path", stored)
        self.assertEqual(self.stored_image_files(), [])
        self.assertEqual(len([image for image in self.image_store.list_images()
                              if image["hash"] == expected]), 1)

    def test_existing_hash_requires_ownership(self):
        data = self.payload()
        del data["image"]
        data["image_hash"] = self.seeded[0]["hash"]
        # Seeded images belong to no account until one ingests them.
        self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
        self.publish_ok()
        self.assertEqual(self.publish.get_ads(self.publish_ok(data))["image_hash"], data["image_hash"])

    def object_service(self, **kwargs):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        return self.service.AdsService(repository, **kwargs)

    def test_object_api_returns_accounts_and_ads(self):
        service = self.object_service()
        account, token = service.create_account("2", "  Second Advertiser  ")
        self.assertIsInstance(account, self.models.AdAccount)
        self.assertEqual(account.to_dict(), {"id": 2, "name": "Second Advertiser"})
        self.assertEqual(service.get_account(2), account)
        ad = service.publish_ad(self.payload(creator_id=2, access_token=token))
        self.assertIsInstance(ad, self.models.Ad)
        self.assertEqual(service.get_ad(ad.id), ad)
        self.assertEqual(self.publish.get_ad(ad.id), ad.to_dict())
        # Dictionary views cannot mutate the domain object.
        view = ad.to_dict()
        view["text"] = "Different copy"
        self.assertEqual(ad.text, VALID_TEXT)

    def test_account_update_and_validation_through_object_api(self):
        service = self.object_service()
        service.rename_account(1, "Updated advertiser", self.token)
        self.assertEqual(service.get_account(1).name, "Updated advertiser")
        for account_id, name, field in ((True, "Advertiser", "account_id"),
                                        (2, "  ", "name")):
            with self.subTest(field=field):
                with self.assertRaises(self.validation.ValidationError) as caught:
                    service.create_account(account_id, name)
                self.assertEqual(caught.exception.field, field)

    def test_custom_validation_rejects_before_image_ingestion(self):
        def reject(draft):
            self.assertIsInstance(draft, self.models.AdDraft)
            raise self.validation.ValidationError("002", "text")

        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        validator = self.validation.AdValidator(repository, rules=[reject])
        publisher = self.publishing.AdPublisher(repository, validator=validator,
                                               image_storage=storage)
        with self.assertRaises(self.validation.ValidationError):
            publisher.publish(self.payload(image=NEW_URL))
        storage.store_image.assert_not_called()
        self.assertIsNone(repository.get_ad(1))

    def test_more_than_one_image_field_rejected_before_ingestion(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        image_hash = self.image_upload(image_name="Hero")["hash"]
        storage = Mock()
        publisher = self.publishing.AdPublisher(repository, image_storage=storage)
        valid = {"image": NEW_URL, "image_hash": image_hash, "image_name": "Hero"}
        # The error names the second field supplied, in link, hash, name order.
        for fields, field in ((("image", "image_hash"), "image_hash"),
                              (("image", "image_name"), "image_name"),
                              (("image_hash", "image_name"), "image_name"),
                              (("image", "image_hash", "image_name"), "image_hash")):
            with self.subTest(fields=fields):
                data = self.payload(**{name: valid[name] for name in fields})
                if "image" not in fields:
                    del data["image"]
                with self.assertRaises(self.validation.ValidationError) as caught:
                    publisher.publish(data)
                self.assertEqual(caught.exception.field, field)
        storage.store_image.assert_not_called()
        self.assertIsNone(repository.get_ad(1))

    def test_custom_image_storage_and_clock(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        storage.store_image.return_value = {"hash": "f" * 32}
        publisher = self.publishing.AdPublisher(repository, image_storage=storage,
                                               clock=lambda: 1234567890)
        service = self.object_service(publisher=publisher)
        ad = service.publish_ad(self.payload(image=NEW_URL))
        self.assertEqual(ad.image_hash, "f" * 32)
        self.assertEqual(ad.creation_time, 1234567890)
        self.assertEqual(service.get_ad(ad.id), ad)
        storage.store_image.assert_called_once_with(NEW_URL)
        # The ingested image is owned from the injected clock's time.
        images, _ = service.list_images(1, self.token)
        self.assertIn(self.models.Image("f" * 32, 1, 1234567890), images)

    def test_custom_publish_stage(self):
        from dataclasses import replace

        class UppercasePublisher(self.publishing.AdPublisher):
            def persist(self, draft):
                return super().persist(replace(draft, text=draft.text.upper()))

        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        publisher = UppercasePublisher(repository)
        ad = publisher.publish(self.payload())
        self.assertEqual(ad.text, VALID_TEXT.upper())
        self.assertEqual(repository.get_ad(ad.id), ad)

    def test_image_ingestion_failure_does_not_publish(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        storage.store_image.side_effect = OSError("Storage unavailable")
        publisher = self.publishing.AdPublisher(repository, image_storage=storage)
        with self.assertRaises(self.validation.ValidationError) as caught:
            publisher.publish(self.payload(image=NEW_URL))
        self.assert_error(caught.exception.to_dict(), "002", "image")
        self.assertIsNone(repository.get_ad(1))

    def test_existing_sqlite_records_remain_readable(self):
        import sqlite3

        # Build a legacy DB independently, without the new repository's schema.
        legacy_path = self.data_dir / "legacy.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO accounts VALUES (1, 'Legacy Advertiser')")
            conn.execute("CREATE TABLE ads (id INTEGER PRIMARY KEY, system_name TEXT, "
                         "image_hash TEXT, text_content TEXT, creator_id INTEGER, creation_time INTEGER)")
            conn.execute(
                "INSERT INTO ads (id, system_name, image_hash, text_content, creator_id, "
                "creation_time) VALUES (?, ?, ?, ?, ?, ?)",
                (7, "ad_7", self.seeded[0]["hash"], VALID_TEXT, 1, 1234567890),
            )
        repository = self.repository.SQLiteAdsRepository(legacy_path)
        service = self.service.AdsService(repository)
        self.assertEqual(service.get_account(1).name, "Legacy Advertiser")
        ad = service.get_ad(7)
        self.assertEqual(ad, self.models.Ad(7, "ad_7", self.seeded[0]["hash"],
                                           VALID_TEXT, 1, 1234567890))
        self.assertIsNone(ad.video_id)
        # Legacy accounts have no token until an operator issues one.
        with self.assertRaises(self.validation.ValidationError) as caught:
            service.publish_ad(self.payload())
        self.assertEqual(caught.exception.field, "access_token")
        token = service.issue_access_token(1)
        self.assertEqual(service.publish_ad(self.payload(access_token=token)).id, 8)
        # The legacy ads table gains video_id in place and accepts video ads.
        video = repository.save_video(1, "Legacy video", "a" * 32, 1234567890)
        video_ad = service.publish_ad({"creator_id": 1, "access_token": token, "text": VALID_TEXT,
                                       "video_id": video.id})
        self.assertEqual(service.get_ad(video_ad.id).video_id, video.id)

    def test_input_shapes_and_required_fields(self):
        for data in (None, [], "invalid", 1):
            with self.subTest(data=data):
                self.assert_error(self.publish.publish_ad(data), "002", "data")
        # Unknown fields, including former text aliases, are rejected.
        for field in ("unknown", "caption"):
            with self.subTest(unknown=field):
                self.assert_error(self.publish.publish_ad(self.payload(**{field: VALID_TEXT})),
                                  "001", field)
        for field in ("text", "creator_id", "image", "access_token"):
            with self.subTest(missing=field):
                data = self.payload()
                del data[field]
                self.assert_error(self.publish.publish_ad(data), "002", field)
        self.assertIsNone(self.publish.get_ad(1))

    def test_hash_validation_boundaries_and_no_ingestion(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        storage.get_image_by_hash.side_effect = lambda image_hash: {"hash": image_hash}
        publisher = self.publishing.AdPublisher(repository, image_storage=storage)
        for value in (None, 123, "", "a" * 15, "a" * 65, "a!" * 16):
            with self.subTest(value=value):
                data = {"creator_id": 1, "access_token": self.token, "text": VALID_TEXT,
                        "image_hash": value}
                with self.assertRaises(self.validation.ValidationError) as caught:
                    publisher.publish(data)
                self.assertEqual(caught.exception.field, "image_hash")
        for value in ("a" * 16, "b" * 64):
            with self.subTest(value=value):
                repository.grant_image(1, value, 1)
                ad = publisher.publish({"creator_id": 1, "access_token": self.token,
                                        "text": VALID_TEXT, "image_hash": value})
                self.assertEqual(ad.image_hash, value)
        storage.store_image.assert_not_called()

    def test_id_and_text_normalization(self):
        created = self.publish.create_account(" 002 ", " Advertiser Two ")
        self.assertEqual(created["id"], 2)
        ad_id = self.publish_ok(self.payload(creator_id=" 002 ", text="  Short copy  ",
                                             access_token=created["access_token"]))
        ad = self.publish.get_ad(str(ad_id))
        self.assertEqual(ad["creator_id"], 2)
        self.assertEqual(ad["text"], "Short copy")
        self.assertEqual(self.publish.get_account("002")["name"], "Advertiser Two")

    def test_unicode_non_integer_digits_rejected(self):
        # isdigit() accepts superscripts, but int() cannot parse them.
        for value in ("²", "①"):
            with self.subTest(value=value):
                self.assert_error(self.publish.create_account(value, "Advertiser"),
                                  "002", "account_id")
                with self.assertRaises(self.validation.ValidationError):
                    self.object_service().create_account(value, "Advertiser")
                self.assertIsNone(self.object_service().get_account(value))
                self.assertIsNone(self.object_service().get_ad(value))

    def test_projection_variants_and_invalid_types(self):
        ad_id = self.publish_ok()
        full = self.publish.get_ad(ad_id)
        for fields in (None, [], (), set()):
            with self.subTest(fields=fields):
                self.assertEqual(self.publish.get_ads(ad_id, fields), full)
        for fields in ("text", ("text",), {"text"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.publish.get_ads(ad_id, fields), {"text": VALID_TEXT})
        self.assertEqual(self.publish.get_ads(ad_id, ["text", "image_hash"]),
                         {"text": VALID_TEXT, "image_hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"})
        for fields in (123, {"text": True}, True):
            with self.subTest(fields=fields):
                self.assert_error(self.publish.get_ads(ad_id, fields), "002", "fields")
        for field in ("unknown", None, 1, [], {}):
            with self.subTest(field=field):
                self.assert_error(self.publish.get_ads(ad_id, [field]), "001", field)

    def test_domain_objects_are_immutable(self):
        from dataclasses import FrozenInstanceError

        objects = (self.models.AdAccount(1, "Advertiser"),
                   self.models.AdDraft(1, VALID_TEXT, image_hash="a" * 32),
                   self.object_service().publish_ad(self.payload()),
                   self.models.Video(1, "Clip", 1, 0, "a" * 32),
                   self.models.Image("a" * 32, 1, 0, "Hero"))
        for obj in objects:
            for field in obj.__dataclass_fields__:
                with self.subTest(object_type=type(obj).__name__, field=field):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(obj, field, "Changed value")

    def test_bad_image_storage_handles_rejected(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        for result in ({}, {"hash": None}, {"hash": 1}, {"hash": ""}, {"hash": "invalid"}):
            with self.subTest(result=result):
                storage = Mock()
                storage.store_image.return_value = result
                publisher = self.publishing.AdPublisher(repository, image_storage=storage)
                with self.assertRaises(self.validation.ValidationError) as caught:
                    publisher.publish(self.payload(image=NEW_URL))
                self.assertEqual(caught.exception.field, "image")
                self.assertIsNone(repository.get_ad(1))

    def test_real_image_registry_write_failure_rejected(self):
        original_replace = os.replace
        def fail_registry_replace(src, dst):
            if str(dst) == self.image_store._PERSIST_FILE:
                raise PermissionError("Registry not writable")
            return original_replace(src, dst)

        with patch.object(self.image_store.os, "replace", side_effect=fail_registry_replace):
            self.assert_error(self.publish.publish_ad(self.payload(image=NEW_URL)),
                              "002", "image")
        self.assertIsNone(self.publish.get_ad(1))
        self.assertIsNone(self.image_store.get_image_by_url(NEW_URL))
        self.assertEqual(list(Path(self.image_store._PERSIST_DIR).glob("*.tmp")), [])

    def test_interrupted_registry_write_keeps_previous_registry(self):
        uploaded = self.image_upload()["hash"]
        registry = Path(self.image_store._PERSIST_FILE)
        before = registry.read_bytes()

        def partial_dump(images, f, **kwargs):
            f.write("[{")  # The process fails midway through writing.
            raise OSError("Disk full")

        with patch.object(self.image_store.json, "dump", side_effect=partial_dump):
            self.assert_error(self.publish.publish_ad(self.payload(image=NEW_URL)),
                              "002", "image")
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(list(Path(self.image_store._PERSIST_DIR).glob("*.tmp")), [])
        data = self.payload(image_hash=uploaded)
        del data["image"]
        self.publish_ok(data)
        self.publish_ok(self.payload(image=NEW_URL))

    def test_repository_failure_mapping(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.get_token_hash.return_value = self.validation.hash_token("token")
        repository.insert_account.side_effect = OSError("DB unavailable")
        repository.save_ad.side_effect = OSError("DB unavailable")
        service = self.service.AdsService(repository)
        data = {"creator_id": 1, "access_token": "token", "text": VALID_TEXT,
                "image_hash": self.seeded[0]["hash"]}
        with self.assertRaises(OSError):
            service.create_account(2, "Advertiser")
        with self.assertRaises(OSError):
            service.publish_ad(data)
        with patch.object(self.publish, "_service", return_value=service):
            self.assert_error(self.publish.create_account(2, "Advertiser"), "002", "account")
            self.assert_error(self.publish.publish_ad(data), "002", "data")
            repository.get_account.side_effect = OSError("DB unavailable")
            repository.get_ad.side_effect = OSError("DB unavailable")
            self.assertIsNone(self.publish.get_account(1))
            self.assertIsNone(self.publish.get_ad(1))

    def test_repository_rolls_back_failed_insert(self):
        import sqlite3

        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        with sqlite3.connect(self.publish.DB_PATH) as conn:
            conn.execute("CREATE TRIGGER reject_ad BEFORE INSERT ON ads BEGIN "
                         "SELECT RAISE(ABORT, 'Rejected insert'); END")
        draft = self.models.AdDraft(1, VALID_TEXT, image_hash="a" * 32)
        with self.assertRaises(sqlite3.IntegrityError):
            repository.save_ad(draft, 1234567890)
        self.assertIsNone(repository.get_ad(1))
        with sqlite3.connect(self.publish.DB_PATH) as conn:
            conn.execute("DROP TRIGGER reject_ad")
        self.assertEqual(repository.save_ad(draft, 1234567890).id, 1)

    def test_concurrent_ad_ids_are_unique_and_persisted(self):
        from concurrent.futures import ThreadPoolExecutor

        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        draft = self.models.AdDraft(1, VALID_TEXT, image_hash="a" * 32)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ads = list(pool.map(lambda _: repository.save_ad(draft, 1234567890), range(12)))
        self.assertEqual(len({ad.id for ad in ads}), 12)
        for ad in ads:
            self.assertEqual(repository.get_ad(ad.id), ad)

    def video_file(self, name="clip.mp4", content=b"FAKE_VIDEO_BYTES"):
        path = self.data_dir / "uploads" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return str(path)

    def video_payload(self, **changes):
        data = {"owner_id": 1, "access_token": self.token, "name": "Launch teaser",
                "file": self.video_file()}
        data.update(changes)
        return data

    def video_repository(self):
        return self.repository.SQLiteAdsRepository(self.publish.DB_PATH)

    def test_upload_video_persists_metadata_and_bytes(self):
        before = int(time.time())
        response = self.publish.upload_video(self.video_payload(name="  Launch teaser  "))
        self.assertEqual(set(response), {"id"})
        video = self.video_repository().get_video(response["id"])
        expected_hash = hashlib.sha256(b"FAKE_VIDEO_BYTES").hexdigest()[:32]
        self.assertEqual((video.name, video.owner_id, video.video_hash),
                         ("Launch teaser", 1, expected_hash))
        self.assertLessEqual(before, video.upload_time)
        self.assertLessEqual(video.upload_time, int(time.time()))
        stored = self.video_store.VIDEO_DIR / expected_hash
        self.assertEqual(stored.read_bytes(), b"FAKE_VIDEO_BYTES")
        self.assertTrue(stored.is_relative_to(self.data_dir))
        # Durable across processes, and the source file is not needed after upload.
        Path(self.video_payload()["file"]).unlink()
        fresh = self.fresh_process(
            "import json, publish; from repository import SQLiteAdsRepository; "
            f"print(json.dumps(SQLiteAdsRepository(publish.DB_PATH).get_video({video.id}).to_dict()))"
        )
        self.assertEqual(fresh, video.to_dict())

    def test_upload_video_ids_unique_and_content_deduplicated(self):
        ids = [self.publish.upload_video(self.video_payload())["id"] for _ in range(2)]
        self.assertNotEqual(*ids)
        self.assertEqual(len(list(self.video_store.VIDEO_DIR.iterdir())), 1)

    def test_upload_video_owner_must_exist(self):
        token = self.create_account(2, "Advertiser Two")
        video_id = self.publish.upload_video(self.video_payload(owner_id="2", access_token=token))["id"]
        self.assertEqual(self.video_repository().get_video(video_id).owner_id, 2)
        for owner in (999999999, None, True, "invalid", "²"):
            with self.subTest(owner=owner):
                self.assert_error(self.publish.upload_video(self.video_payload(owner_id=owner)),
                                  "002", "owner_id")

    def test_upload_video_input_validation(self):
        self.video_file("notes.txt")
        directory = self.data_dir / "uploads" / "folder.mp4"
        directory.mkdir(parents=True)
        cases = [(data, "002", "data") for data in (None, [], "invalid")]
        cases.append((self.video_payload(unknown=1), "001", "unknown"))
        for field in ("owner_id", "access_token", "name", "file"):
            data = self.video_payload()
            del data[field]
            cases.append((data, "002", field))
        for name in ("", "  ", None, 123):
            cases.append((self.video_payload(name=name), "002", "name"))
        for file in ("", None, str(self.data_dir / "missing.mp4"), str(directory),
                     str(self.data_dir / "uploads" / "notes.txt"), "https://cdn.example.com/a.mp4"):
            cases.append((self.video_payload(file=file), "002", "file"))
        for data, code, field in cases:
            with self.subTest(data=data):
                self.assert_error(self.publish.upload_video(data), code, field)
        self.assertIsNone(self.video_repository().get_video(1))
        self.assertFalse(self.video_store.VIDEO_DIR.exists())

    def test_upload_video_storage_failure_does_not_persist(self):
        storage = Mock()
        storage.store_video.side_effect = OSError("Disk full")
        service = self.service.AdsService(self.video_repository(), video_storage=storage)
        with self.assertRaises(self.validation.ValidationError) as caught:
            service.upload_video(self.video_payload())
        self.assert_error(caught.exception.to_dict(), "002", "file")
        self.assertIsNone(self.video_repository().get_video(1))

    def test_failed_copy_leaves_no_partial_video(self):
        # Fail mid-copy, after the temporary file exists.
        digest = Mock()
        digest.update.side_effect = RuntimeError("Read failed")
        with patch.object(self.file_store, "hashlib", Mock(sha256=Mock(return_value=digest))):
            self.assert_error(self.publish.upload_video(self.video_payload()), "002", "file")
        self.assertEqual(list(self.video_store.VIDEO_DIR.iterdir()), [])

    def test_rename_video_changes_only_name(self):
        video_id = self.publish.upload_video(self.video_payload())["id"]
        original = self.video_repository().get_video(video_id)
        self.assertEqual(self.publish.rename_video(str(video_id), "  Final cut  ", self.token),
                         {"id": video_id})
        renamed = self.video_repository().get_video(video_id)
        self.assertEqual(renamed, self.models.Video(video_id, "Final cut", original.owner_id,
                                                    original.upload_time, original.video_hash))
        other_token = self.create_account(2, "Advertiser Two")
        # A wrong token looks the same as a missing video.
        for video_id_value, name, token, field in (
                (999999, "Name", self.token, "video_id"), ("x", "Name", self.token, "video_id"),
                (True, "Name", self.token, "video_id"), (video_id, " ", self.token, "name"),
                (video_id, "Name", None, "video_id"), (video_id, "Name", other_token, "video_id")):
            with self.subTest(video_id=video_id_value, name=name, token=token):
                self.assert_error(self.publish.rename_video(video_id_value, name, token), "002", field)
        self.assertEqual(self.video_repository().get_video(video_id).name, "Final cut")

    def seed_videos(self, upload_times, owner_id=1):
        repository = self.video_repository()
        return [repository.save_video(owner_id, f"Video {i}", "a" * 32, upload_time).to_dict()
                for i, upload_time in enumerate(upload_times)]

    def list_all_pages(self, **kwargs):
        pages, cursor = [], None
        # Bounded so a cursor that never ends fails instead of hanging.
        for _ in range(50):
            page = self.publish.list_videos(1, cursor=cursor, **kwargs)
            self.assertEqual(set(page), {"data", "next_cursor"}, page)
            pages.append(page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                return pages
        self.fail("pagination did not end")

    def test_list_videos_paginates_newest_first(self):
        self.publish.create_account(2, "Other Advertiser")
        # Equal upload times are ordered by ID, newest first.
        videos = self.seed_videos([100, 300, 200, 300, 100])
        self.seed_videos([250], owner_id=2)
        expected = [videos[i] for i in (3, 1, 2, 4, 0)]
        pages = self.list_all_pages(limit=2)
        self.assertEqual([len(page) for page in pages], [2, 2, 1])
        self.assertEqual([video for page in pages for video in page], expected)
        self.assertEqual(self.publish.list_videos(1, limit="5"),
                         {"data": expected, "next_cursor": None})

    def test_list_videos_default_page_size(self):
        videos = self.seed_videos(range(26))
        first = self.publish.list_videos(1)
        self.assertEqual(first["data"], videos[:0:-1])
        second = self.publish.list_videos(1, cursor=first["next_cursor"])
        self.assertEqual(second, {"data": [videos[0]], "next_cursor": None})

    def test_list_videos_filters_by_upload_time(self):
        videos = self.seed_videos([100, 200, 200, 300, 400])
        # since is inclusive and until is exclusive.
        pages = self.list_all_pages(limit=1, since=200, until="400")
        self.assertEqual([video for page in pages for video in page],
                         [videos[3], videos[2], videos[1]])
        self.assertEqual(self.publish.list_videos(1, since=401)["data"], [])
        self.assertEqual(self.publish.list_videos(1, since=300, until=300)["data"], [])
        self.assertEqual(self.publish.list_videos(1, until=101)["data"], [videos[0]])

    def test_list_videos_pages_stable_across_new_uploads(self):
        videos = self.seed_videos([100, 200, 300])
        first = self.publish.list_videos(1, limit=2)
        self.seed_videos([500])
        second = self.publish.list_videos(1, limit=2, cursor=first["next_cursor"])
        self.assertEqual(first["data"] + second["data"], [videos[2], videos[1], videos[0]])

    def test_list_videos_empty_and_object_api(self):
        self.assertEqual(self.publish.list_videos(1), {"data": [], "next_cursor": None})
        uploaded = self.service.AdsService(self.video_repository()).upload_video(self.video_payload())
        videos, next_cursor = self.object_service().list_videos(1)
        self.assertEqual((videos, next_cursor), ([uploaded], None))
        self.assertIsInstance(videos[0], self.models.Video)

    def test_list_videos_rejects_invalid_parameters(self):
        self.seed_videos([100])
        cases = [({"owner_id": owner}, "owner_id") for owner in (999999999, None, True, "x", "²")]
        cases += [({"limit": limit}, "limit") for limit in (0, 101, -1, "x", True, 1.5)]
        cases += [({"cursor": cursor}, "cursor")
                  for cursor in ("", "abc", "1:2:3", "a:b", "1:", 5, "-1:2", f"{2**63}:1")]
        for field in ("since", "until"):
            cases += [({field: value}, field) for value in (-1, "x", True, 1.5, 2**63)]
        for kwargs, field in cases:
            with self.subTest(kwargs=kwargs):
                kwargs = {"owner_id": 1, **kwargs}
                self.assert_error(self.publish.list_videos(**kwargs), "002", field)
                with self.assertRaises(self.validation.ValidationError):
                    self.object_service().list_videos(**kwargs)

    def test_list_videos_storage_failure(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.list_videos.side_effect = OSError("DB unavailable")
        with patch.object(self.publish, "_service",
                          return_value=self.service.AdsService(repository)):
            self.assert_error(self.publish.list_videos(1), "002", "videos")

    def video_ad_payload(self, video_id, **changes):
        data = {"creator_id": 1, "access_token": self.token, "text": VALID_TEXT, "video_id": video_id}
        data.update(changes)
        return data

    def test_publish_video_ad(self):
        video_id = self.publish.upload_video(self.video_payload())["id"]
        before = int(time.time())
        ad_id = self.publish_ok(self.video_ad_payload(str(video_id)))
        ad = self.publish.get_ad(ad_id)
        self.assertLessEqual(before, ad["creation_time"])
        self.assertEqual(ad, {"id": ad_id, "system_name": f"ad_{ad_id}", "image_hash": None,
                              "text": VALID_TEXT, "creator_id": 1,
                              "creation_time": ad["creation_time"], "video_id": video_id})
        self.assertEqual(self.publish.get_ads(ad_id, ["video_id"]), {"video_id": video_id})
        fresh = self.fresh_process(f"import json, publish; print(json.dumps(publish.get_ad({ad_id})))")
        self.assertEqual(fresh, ad)

    def test_video_ad_requires_creator_to_own_video(self):
        token = self.create_account(2, "Advertiser Two")
        other_video = self.publish.upload_video(self.video_payload(owner_id=2, access_token=token))["id"]
        # Missing and unowned videos are indistinguishable to the caller.
        for video_id in (other_video, 999999, None, True, "x", "²", 1.5, 2**63):
            with self.subTest(video_id=video_id):
                self.assert_error(self.publish.publish_ad(self.video_ad_payload(video_id)),
                                  "002", "video_id")
        self.assertIsNone(self.publish.get_ad(1))
        # The owner can use it.
        ad_id = self.publish_ok(self.video_ad_payload(other_video, creator_id=2, access_token=token))
        self.assertEqual(self.publish.get_ad(ad_id)["video_id"], other_video)

    def test_video_ad_rejects_image_fields(self):
        video_id = self.publish.upload_video(self.video_payload())["id"]
        storage = Mock()
        publisher = self.publishing.AdPublisher(self.video_repository(), image_storage=storage)
        for extra in ({"image": NEW_URL}, {"image_hash": "a" * 32}):
            with self.subTest(extra=extra):
                with self.assertRaises(self.validation.ValidationError) as caught:
                    publisher.publish(self.video_ad_payload(video_id, **extra))
                self.assert_error(caught.exception.to_dict(), "002", "video_id")
        storage.store_image.assert_not_called()
        self.assertIsNone(self.publish.get_ad(1))

    def test_video_lookup_failure_rejects_video_ad(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.get_token_hash.return_value = self.validation.hash_token(self.token)
        repository.get_video.side_effect = OSError("DB unavailable")
        with self.assertRaises(self.validation.ValidationError) as caught:
            self.service.AdsService(repository).publish_ad(self.video_ad_payload(1))
        self.assert_error(caught.exception.to_dict(), "002", "video_id")
        repository.save_ad.assert_not_called()

    def image_upload(self, **changes):
        data = {"owner_id": 1, "access_token": self.token,
                "file": self.video_file("photo.png", b"PNG_BYTES")}
        data.update(changes)
        return self.publish.upload_image(data)

    def stored_image_files(self):
        return sorted(path.name for path in Path(self.image_store._PERSIST_DIR).iterdir()
                      if path.name != "persisted_images.json")

    def test_account_token_is_hashed_and_guards_renames(self):
        import sqlite3

        with sqlite3.connect(self.publish.DB_PATH) as conn:
            stored = conn.execute("SELECT token_hash FROM accounts WHERE id = 1").fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(self.token.encode()).hexdigest())
        # Creating an existing ID needs no token and must not replace the account.
        self.assert_error(self.publish.create_account(1, "Hijacked"), "002", "account_id")
        for token in ("wrong", "", 123, "é", self.create_account(2, "Advertiser Two")):
            with self.subTest(token=token):
                self.assert_error(self.publish.create_account(1, "Hijacked", token),
                                  "002", "access_token")
        self.assertEqual(self.publish.get_account(1)["name"], "Advertiser One")
        self.assertEqual(self.publish.create_account(1, " Renamed ", self.token), {"id": 1})
        self.assertEqual(self.publish.get_account(1)["name"], "Renamed")
        # Renaming keeps the token.
        self.publish_ok()

    def test_issue_access_token_replaces_old_token(self):
        service = self.object_service()
        token = service.issue_access_token("1")
        self.assert_error(self.publish.publish_ad(self.payload()), "002", "access_token")
        self.publish_ok(self.payload(access_token=token))
        for account_id in (999999, "x", None):
            with self.subTest(account_id=account_id):
                with self.assertRaises(self.validation.ValidationError) as caught:
                    service.issue_access_token(account_id)
                self.assertEqual(caught.exception.field, "account_id")

    def test_publish_requires_creator_token(self):
        other_token = self.create_account(2, "Advertiser Two")
        cases = [self.payload(access_token=token) for token in (None, "wrong", 123, other_token)]
        missing = self.payload()
        del missing["access_token"]
        cases.append(missing)
        # Authentication runs first, so unowned images and videos reveal nothing.
        hash_only = self.payload(access_token="wrong", image_hash="a" * 32)
        del hash_only["image"]
        cases.append(hash_only)
        cases.append(self.video_ad_payload(999999, access_token="wrong"))
        for data in cases:
            with self.subTest(data=data):
                self.assert_error(self.publish.publish_ad(data), "002", "access_token")
        self.assertIsNone(self.publish.get_ad(1))

    def test_upload_image_returns_owned_content_hash(self):
        expected = hashlib.sha256(b"PNG_BYTES").hexdigest()[:32]
        self.assertEqual(self.image_upload(), {"hash": expected})
        # The copy lives in the data directory and is found by hash in a new process.
        stored = self.fresh_process(
            f"import json, image_store; print(json.dumps(image_store.get_image_by_hash({expected!r})))")
        self.assertEqual(Path(stored["path"]).read_bytes(), b"PNG_BYTES")
        self.assertTrue(Path(stored["path"]).is_relative_to(self.data_dir))
        data = self.payload(image_hash=expected)
        del data["image"]
        self.assertEqual(self.publish.get_ad(self.publish_ok(data))["image_hash"], expected)
        # Another account cannot use the image until it uploads the same bytes itself.
        other_token = self.create_account(2, "Advertiser Two")
        other = dict(data, creator_id=2, access_token=other_token)
        self.assert_error(self.publish.publish_ad(other), "002", "image_hash")
        self.assertEqual(self.image_upload(owner_id=2, access_token=other_token), {"hash": expected})
        self.publish_ok(other)
        # Same bytes, same hash: one registry entry and one stored copy.
        self.assertEqual(len([image for image in self.image_store.list_images()
                              if image["hash"] == expected]), 1)
        self.assertIn(f"{expected}.png", self.stored_image_files())

    def test_upload_image_rejects_before_storing(self):
        directory = self.data_dir / "uploads" / "folder.png"
        directory.mkdir(parents=True)
        cases = [(data, "002", "data") for data in (None, [], "invalid")]
        cases.append(({"owner_id": 1, "access_token": self.token, "file": "x", "name": "n"},
                      "001", "name"))
        for field in ("owner_id", "access_token", "file"):
            data = {"owner_id": 1, "access_token": self.token, "file": self.video_file("a.png")}
            del data[field]
            cases.append((data, "002", field))
        for owner in (999999999, None, True, "x"):
            cases.append(({"owner_id": owner, "access_token": self.token, "file": "x"},
                          "002", "owner_id"))
        for token in (None, "wrong", 123, self.create_account(2, "Advertiser Two")):
            cases.append(({"owner_id": 1, "access_token": token,
                           "file": self.video_file("b.png")}, "002", "access_token"))
        for file in ("", None, str(self.data_dir / "missing.png"), str(directory)):
            cases.append(({"owner_id": 1, "access_token": self.token, "file": file},
                          "002", "file"))
        for data, code, field in cases:
            with self.subTest(data=data):
                self.assert_error(self.publish.upload_image(data), code, field)
        self.assertEqual(self.stored_image_files(), [])

    def test_image_and_video_size_limits(self):
        # Images: the real 30 MiB limit, exactly at and one byte over it.
        limit = 30 * 1024 * 1024
        at_image_limit = b"\0" * limit
        fits = self.image_upload(file=self.video_file("fits.png", at_image_limit))
        self.assertEqual(fits, {"hash": hashlib.sha256(at_image_limit).hexdigest()[:32]})
        self.assert_error(self.image_upload(file=self.video_file("big.png", at_image_limit + b"\0")),
                          "002", "file")
        self.assertEqual(self.stored_image_files(), [f"{fits['hash']}.png"])
        # Videos: a 1 GiB fixture is impractical, so pin the value and enforce a small limit.
        self.assertEqual(self.video_store.MAX_VIDEO_BYTES, 1024 ** 3)
        at_limit, over_limit = b"12345678", b"123456789"
        with patch.object(self.video_store, "MAX_VIDEO_BYTES", 8):
            self.assertIn("id", self.publish.upload_video(
                self.video_payload(file=self.video_file("fits.mp4", at_limit))))
            self.assert_error(self.publish.upload_video(
                self.video_payload(file=self.video_file("big.mp4", over_limit))), "002", "file")
        self.assertEqual([path.name for path in self.video_store.VIDEO_DIR.iterdir()],
                         [hashlib.sha256(at_limit).hexdigest()[:32]])
        self.assertIsNone(self.video_repository().get_video(2))

    def seed_images(self, creation_times, owner_id=1):
        # Hash i is letter i repeated, so ties order by index descending.
        repository = self.video_repository()
        images = []
        for i, creation_time in enumerate(creation_times):
            image_hash = "abcdefghij"[i] * 32
            repository.grant_image(owner_id, image_hash, creation_time)
            images.append({"hash": image_hash, "owner_id": owner_id,
                           "creation_time": creation_time, "image_name": None})
        return images

    def list_all_image_pages(self, **kwargs):
        pages, cursor = [], None
        # Bounded so a cursor that never ends fails instead of hanging.
        for _ in range(50):
            page = self.publish.list_images(1, self.token, cursor=cursor, **kwargs)
            self.assertEqual(set(page), {"data", "next_cursor"}, page)
            pages.append(page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                return pages
        self.fail("pagination did not end")

    def test_list_images_paginates_newest_first(self):
        self.create_account(2, "Other Advertiser")
        # Equal creation times are ordered by hash, descending.
        images = self.seed_images([100, 300, 200, 300, 100])
        self.seed_images([250], owner_id=2)
        expected = [images[i] for i in (3, 1, 2, 4, 0)]
        pages = self.list_all_image_pages(limit=2)
        self.assertEqual([len(page) for page in pages], [2, 2, 1])
        self.assertEqual([image for page in pages for image in page], expected)
        self.assertEqual(self.publish.list_images(1, self.token, limit="5"),
                         {"data": expected, "next_cursor": None})

    def test_list_images_filters_by_creation_time(self):
        images = self.seed_images([100, 200, 200, 300, 400])
        # since is inclusive and until is exclusive.
        pages = self.list_all_image_pages(limit=1, since=200, until="400")
        self.assertEqual([image for page in pages for image in page],
                         [images[3], images[2], images[1]])
        self.assertEqual(self.publish.list_images(1, self.token, since=401)["data"], [])
        self.assertEqual(self.publish.list_images(1, self.token, since=300, until=300)["data"], [])
        self.assertEqual(self.publish.list_images(1, self.token, until=101)["data"], [images[0]])

    def test_list_images_pages_stable_across_new_images(self):
        images = self.seed_images([100, 200, 300])
        first = self.publish.list_images(1, self.token, limit=2)
        self.video_repository().grant_image(1, "z" * 32, 500)
        second = self.publish.list_images(1, self.token, limit=2, cursor=first["next_cursor"])
        self.assertEqual(first["data"] + second["data"], [images[2], images[1], images[0]])

    def test_list_images_requires_owner_token(self):
        other_token = self.create_account(2, "Advertiser Two")
        self.seed_images([100])
        for token in (None, "", "wrong", 123, other_token):
            with self.subTest(token=token):
                self.assert_error(self.publish.list_images(1, token), "002", "access_token")
        for owner in (999999999, None, True, "x", "²"):
            with self.subTest(owner=owner):
                self.assert_error(self.publish.list_images(owner, self.token), "002", "owner_id")
        # An account's own token lists only its own images.
        self.assertEqual(self.publish.list_images(2, other_token),
                         {"data": [], "next_cursor": None})

    def test_list_images_shows_uploads_and_ingested_images(self):
        before = int(time.time())
        uploaded = self.image_upload()["hash"]
        ingested = self.publish.get_ad(self.publish_ok())["image_hash"]
        images, next_cursor = self.object_service().list_images("1", self.token)
        self.assertIsNone(next_cursor)
        self.assertEqual({image.hash for image in images}, {uploaded, ingested})
        for image in images:
            self.assertIsInstance(image, self.models.Image)
            self.assertEqual(image.owner_id, 1)
            self.assertLessEqual(before, image.creation_time)
            self.assertLessEqual(image.creation_time, int(time.time()))
        # Uploading the same bytes again keeps the original creation time.
        with patch.object(self.service.time, "time", return_value=before + 1000):
            self.assertEqual(self.image_upload(), {"hash": uploaded})
        self.assertEqual(self.object_service().list_images(1, self.token)[0], images)

    def test_list_images_rejects_invalid_parameters(self):
        self.seed_images([100])
        valid_hash = "a" * 32
        cases = [({"limit": limit}, "limit") for limit in (0, 101, -1, "x", True, 1.5)]
        cases += [({"cursor": cursor}, "cursor")
                  for cursor in ("", "abc", "1:2", "1:", f":{valid_hash}", f"x:{valid_hash}",
                                 f"-1:{valid_hash}", f"{2**63}:{valid_hash}", f"1:{'a' * 15}",
                                 f"1:{valid_hash}\n", f"1:{valid_hash}:2", 5)]
        for field in ("since", "until"):
            cases += [({field: value}, field) for value in (-1, "x", True, 1.5, 2**63)]
        for kwargs, field in cases:
            with self.subTest(kwargs=kwargs):
                self.assert_error(self.publish.list_images(1, self.token, **kwargs), "002", field)
                with self.assertRaises(self.validation.ValidationError):
                    self.object_service().list_images(1, self.token, **kwargs)

    def test_list_images_storage_failure(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.get_token_hash.return_value = self.validation.hash_token(self.token)
        repository.list_images.side_effect = OSError("DB unavailable")
        with patch.object(self.publish, "_service",
                          return_value=self.service.AdsService(repository)):
            self.assert_error(self.publish.list_images(1, self.token), "002", "images")

    def test_publish_rejects_hashes_missing_from_image_store(self):
        data = self.payload(image_hash="f" * 32)
        del data["image"]
        # Owned but never stored: no image to show, so the ad must not publish.
        self.video_repository().grant_image(1, "f" * 32, 1)
        self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
        # An uploaded image that later disappears from storage is rejected too.
        uploaded = self.image_upload()["hash"]
        data["image_hash"] = uploaded
        with patch.object(self.image_store, "get_image_by_hash", return_value=None):
            self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
        with patch.object(self.image_store, "get_image_by_hash", side_effect=OSError("down")):
            self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
        self.assertIsNone(self.publish.get_ad(1))
        self.publish_ok(data)

    def test_validator_checks_the_publishers_image_storage(self):
        repository = self.video_repository()
        repository.grant_image(1, "f" * 32, 1)
        storage = Mock()
        storage.get_image_by_hash.return_value = None
        publisher = self.publishing.AdPublisher(repository, image_storage=storage)
        data = self.payload(image_hash="f" * 32)
        del data["image"]
        with self.assertRaises(self.validation.ValidationError) as caught:
            publisher.publish(data)
        self.assertEqual(caught.exception.field, "image_hash")
        storage.get_image_by_hash.assert_called_once_with("f" * 32)
        storage.get_image_by_hash.return_value = {"hash": "f" * 32}
        self.assertEqual(publisher.publish(data).image_hash, "f" * 32)

    def test_uploaded_hashes_publish_unchanged(self):
        hashes = []
        for name, content in (("a.png", b"PNG"), ("b.jpg", b"JPEG"), ("noext", b"RAW"),
                              ("empty.gif", b"")):
            with self.subTest(name=name):
                image_hash = self.image_upload(file=self.video_file(name, content))["hash"]
                self.assertEqual(image_hash, hashlib.sha256(content).hexdigest()[:32])
                self.assertEqual(self.image_store.get_image_by_hash(image_hash)["hash"], image_hash)
                data = self.payload(image_hash=image_hash)
                del data["image"]
                ad = self.publish.get_ad(self.publish_ok(data))
                self.assertEqual(ad["image_hash"], image_hash)
                hashes.append(image_hash)
        # Surrounding spaces are trimmed; hashes are otherwise matched exactly.
        data = self.payload(image_hash=f"  {hashes[0]}  ")
        del data["image"]
        self.assertEqual(self.publish.get_ad(self.publish_ok(data))["image_hash"], hashes[0])
        data["image_hash"] = hashes[0].upper()
        self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
        # Hashes from link ingestion during publish also republish by hash.
        ingested = self.publish.get_ad(self.publish_ok(self.payload(image=NEW_URL)))
        data = self.payload(image_hash=ingested["image_hash"])
        del data["image"]
        self.publish_ok(data)
        fresh = self.fresh_process(
            "import json, publish; print(json.dumps(publish.publish_ad({"
            f"'creator_id': 1, 'access_token': {self.token!r}, 'text': {VALID_TEXT!r}, "
            f"'image_hash': {hashes[1]!r}" + "})))"
        )
        self.assertIn("id", fresh)

    def test_damaged_image_registry_is_not_overwritten(self):
        uploaded = self.image_upload()["hash"]
        registry = Path(self.image_store._PERSIST_FILE)
        for damaged in ("{not json", "{}"):
            with self.subTest(damaged=damaged):
                registry.write_text(damaged)
                self.assert_error(self.image_upload(file=self.video_file("new.png", b"NEW")),
                                  "002", "file")
                self.assert_error(self.publish.publish_ad(self.payload(image=NEW_URL)),
                                  "002", "image")
                data = self.payload(image_hash=uploaded)
                del data["image"]
                self.assert_error(self.publish.publish_ad(data), "002", "image_hash")
                self.assertEqual(registry.read_text(), damaged)
        self.assertIsNone(self.publish.get_ad(1))

    def listed_images(self, owner_id=1, token=None):
        page = self.publish.list_images(owner_id, token or self.token)
        return {image["hash"]: image for image in page["data"]}

    def test_upload_image_sets_optional_name(self):
        named = self.image_upload(image_name="  Spring hero  ")["hash"]
        unnamed = self.image_upload(file=self.video_file("plain.png", b"PLAIN"))["hash"]
        ingested = self.publish.get_ad(self.publish_ok())["image_hash"]
        images = self.listed_images()
        self.assertEqual(images[named]["image_name"], "Spring hero")
        self.assertIsNone(images[unnamed]["image_name"])
        self.assertIsNone(images[ingested]["image_name"])
        self.assertEqual(set(images[named]), {"hash", "owner_id", "creation_time", "image_name"})
        fresh = self.fresh_process(
            f"import json, publish; print(json.dumps(publish.list_images(1, {self.token!r})))")
        self.assertIn(images[named], fresh["data"])

    def test_reupload_updates_name_and_keeps_creation_time(self):
        image_hash = self.image_upload(image_name="First")["hash"]
        created = self.listed_images()[image_hash]["creation_time"]
        with patch.object(self.service.time, "time", return_value=created + 1000):
            self.image_upload(image_name="Second")
            self.assertEqual(self.listed_images()[image_hash],
                             {"hash": image_hash, "owner_id": 1, "creation_time": created,
                              "image_name": "Second"})
            # Without a name, a re-upload keeps the current one.
            self.image_upload()
        self.assertEqual(self.listed_images()[image_hash]["image_name"], "Second")

    def test_image_names_are_per_account(self):
        other_token = self.create_account(2, "Advertiser Two")
        image_hash = self.image_upload(image_name="Ours")["hash"]
        self.assertEqual(self.image_upload(owner_id=2, access_token=other_token,
                                           image_name="Theirs"), {"hash": image_hash})
        self.assertEqual(self.listed_images()[image_hash]["image_name"], "Ours")
        self.assertEqual(self.listed_images(2, other_token)[image_hash]["image_name"], "Theirs")

    def test_upload_image_rejects_invalid_names(self):
        image_hash = self.image_upload(image_name="Original")["hash"]
        for name in ("", "   ", None, 123, ["Name"]):
            with self.subTest(name=name):
                self.assert_error(self.image_upload(image_name=name), "002", "image_name")
                self.assert_error(self.image_upload(file=self.video_file("other.png", b"OTHER"),
                                                    image_name=name), "002", "image_name")
        self.assertEqual(list(self.listed_images()), [image_hash])
        self.assertEqual(self.listed_images()[image_hash]["image_name"], "Original")
        # Videos keep their own name field and do not accept image_name.
        self.assert_error(self.publish.upload_video(self.video_payload(image_name="x")),
                          "001", "image_name")

    def name_payload(self, name, **changes):
        data = self.payload(image_name=name, **changes)
        del data["image"]
        return data

    def test_publish_by_image_name(self):
        image_hash = self.image_upload(image_name="Spring hero")["hash"]
        ad_id = self.publish_ok(self.name_payload("  Spring hero  "))
        self.assertEqual(self.publish.get_ad(ad_id)["image_hash"], image_hash)
        fresh = self.fresh_process(
            "import json, publish; print(json.dumps(publish.publish_ad({"
            f"'creator_id': 1, 'access_token': {self.token!r}, 'text': {VALID_TEXT!r}, "
            "'image_name': 'Spring hero'})))")
        self.assertEqual(self.publish.get_ad(fresh["id"])["image_hash"], image_hash)

    def test_publish_by_image_name_rejects_unusable_names(self):
        self.image_upload(image_name="Spring hero")
        other_token = self.create_account(2, "Advertiser Two")
        self.image_upload(owner_id=2, access_token=other_token,
                          file=self.video_file("theirs.png", b"THEIRS"), image_name="Theirs")
        # Unknown, another account's, and differently cased names look the same.
        for name in ("Unknown", "Theirs", "spring hero", "", "   ", None, 123):
            with self.subTest(name=name):
                self.assert_error(self.publish.publish_ad(self.name_payload(name)),
                                  "002", "image_name")
        # A named image whose stored copy is gone cannot be published.
        with patch.object(self.image_store, "get_image_by_hash", return_value=None):
            self.assert_error(self.publish.publish_ad(self.name_payload("Spring hero")),
                              "002", "image_name")
        # Authentication comes first, so names reveal nothing without the token.
        self.assert_error(self.publish.publish_ad(self.name_payload("Spring hero",
                                                                    access_token="wrong")),
                          "002", "access_token")
        self.assert_error(self.publish.publish_ad(self.name_payload("Spring hero",
                                                                    video_id=1)),
                          "002", "video_id")
        self.assertIsNone(self.publish.get_ad(1))

    def test_image_name_lookup_failure_rejects_publish(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.get_token_hash.return_value = self.validation.hash_token(self.token)
        repository.get_image_hash_by_name.side_effect = OSError("DB unavailable")
        with self.assertRaises(self.validation.ValidationError) as caught:
            self.service.AdsService(repository).publish_ad(self.name_payload("Spring hero"))
        self.assertEqual(caught.exception.field, "image_name")
        repository.save_ad.assert_not_called()

    def test_image_names_unique_per_account(self):
        first = self.image_upload(image_name="Hero")["hash"]
        second_file = self.video_file("second.png", b"SECOND")
        self.assert_error(self.image_upload(file=second_file, image_name=" Hero "),
                          "002", "image_name")
        self.assertEqual(list(self.listed_images()), [first])
        # Re-uploading the same image under its own name is fine.
        self.assertEqual(self.image_upload(image_name="Hero"), {"hash": first})
        # Another account may use the same name.
        other_token = self.create_account(2, "Advertiser Two")
        self.image_upload(owner_id=2, access_token=other_token, file=second_file,
                          image_name="Hero")
        # Renaming the first image frees its old name.
        self.image_upload(image_name="Old hero")
        second = self.image_upload(file=second_file, image_name="Hero")["hash"]
        names = {image_hash: image["image_name"] for image_hash, image in self.listed_images().items()}
        self.assertEqual(names, {first: "Old hero", second: "Hero"})
        self.assertEqual(self.publish.get_ad(self.publish_ok(self.name_payload("Hero")))["image_hash"],
                         second)
        # Unnamed images are exempt from uniqueness.
        self.image_upload(file=self.video_file("u1.png", b"U1"))
        self.image_upload(file=self.video_file("u2.png", b"U2"))
        self.assertEqual(len(self.listed_images()), 4)

    def test_storage_failures_map_to_error_dicts(self):
        import sqlite3

        video_id = self.publish.upload_video(self.video_payload())["id"]
        with sqlite3.connect(self.publish.DB_PATH) as conn:
            for table, statement in (("image_owners", "INSERT"), ("videos", "INSERT"),
                                     ("videos", "UPDATE")):
                conn.execute(f"CREATE TRIGGER reject_{table}_{statement} BEFORE {statement} "
                             f"ON {table} BEGIN SELECT RAISE(ABORT, 'Rejected write'); END")
        # A failed ownership write is a storage failure, not a name conflict.
        self.assert_error(self.image_upload(image_name="Hero"), "002", "data")
        self.assert_error(self.publish.upload_video(self.video_payload()), "002", "data")
        self.assert_error(self.publish.rename_video(video_id, "New name", self.token),
                          "002", "video")
        self.assertEqual(self.listed_images(), {})
        self.assertEqual(self.video_repository().get_video(video_id).name, "Launch teaser")
        self.assertIsNone(self.video_repository().get_video(video_id + 1))

    def test_failed_lookups_reject_writes(self):
        image_hash = self.image_upload()["hash"]
        by_hash = self.payload(image_hash=image_hash)
        del by_hash["image"]
        upload = {"owner_id": 1, "access_token": self.token,
                  "file": self.video_file("new.png", b"NEW")}
        # Authentication and ownership checks fail closed when storage cannot answer.
        for method, call, data, field in (
                ("get_account", "publish_ad", by_hash, "creator_id"),
                ("get_token_hash", "publish_ad", by_hash, "access_token"),
                ("owns_image", "publish_ad", by_hash, "image_hash"),
                ("get_account", "upload_image", upload, "owner_id"),
                ("get_token_hash", "upload_image", upload, "access_token"),
                ("get_token_hash", "upload_video", self.video_payload(), "access_token")):
            with self.subTest(method=method, call=call):
                repository = self.video_repository()
                with patch.object(repository, method, side_effect=OSError("DB unavailable")):
                    with self.assertRaises(self.validation.ValidationError) as caught:
                        getattr(self.service.AdsService(repository), call)(data)
                self.assertEqual(caught.exception.field, field)
        self.assertIsNone(self.publish.get_ad(1))
        self.assertEqual(list(self.listed_images()), [image_hash])
        self.assertIsNone(self.video_repository().get_video(1))

if __name__ == "__main__":
    unittest.main()
