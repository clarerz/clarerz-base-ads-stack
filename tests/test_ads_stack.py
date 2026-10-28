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

    def test_valid_publish_returns_numeric_id(self):
        ad_id = self.publish_ok()
        self.assertIsInstance(ad_id, int)
        self.assertNotIsInstance(ad_id, bool)

    def test_both_url_and_file_image_supported(self):
        for image in (self.seeded[0]["url"], "sample.jpg"):
            with self.subTest(image=image):
                self.publish_ok(self.payload(image=image))

    def test_create_account_and_reference(self):
        token = self.create_account(2, "Advertiser Two")
        ad_id = self.publish_ok(self.payload(creator_id=2, access_token=token))
        self.assertEqual(self.publish.get_ads(ad_id)["creator_id"], 2)

    def test_publish_requires_valid_creator(self):
        for creator in (999999999, None, True, "invalid"):
            with self.subTest(creator=creator):
                self.assert_error(self.publish.publish_ad(self.payload(creator_id=creator)),
                                  "002", "creator_id")
        data = self.payload()
        del data["creator_id"]
        self.assert_error(self.publish.publish_ad(data), "002", "creator_id")

    def test_persistence_via_read_contains_values(self):
        ad = self.publish.get_ads(self.publish_ok())
        self.assertEqual(ad["text"], VALID_TEXT)
        self.assertEqual(ad["image_hash"], self.seeded[0]["hash"])

    def test_persistence_survives_restart(self):
        ad_id = self.publish_ok()
        result = self.fresh_process(
            "import json, publish; print(json.dumps({"
            f"'account': publish.get_account(1), 'ad': publish.get_ads({ad_id})" + "}))"
        )
        self.assertEqual(result["account"], self.publish.get_account(1))
        self.assertEqual(result["ad"], self.publish.get_ads(ad_id))

    def test_ad_system_name_template(self):
        ad_id = self.publish_ok()
        self.assertEqual(self.publish.get_ads(ad_id)["system_name"], f"ad_{ad_id}")

    def test_url_normalized_to_hash(self):
        ad = self.publish.get_ads(self.publish_ok())
        self.assertEqual(ad["image_hash"], self.seeded[0]["hash"])
        self.assertNotIn(self.seeded[0]["url"], ad.values())

    def test_metadata_creation_time_and_creator(self):
        before = int(time.time())
        ad = self.publish.get_ads(self.publish_ok())
        self.assertEqual(ad["creator_id"], 1)
        self.assertIsInstance(ad["creation_time"], int)
        self.assertLessEqual(before, ad["creation_time"])
        self.assertLessEqual(ad["creation_time"], int(time.time()))

    def test_read_path_projection_valid(self):
        ad_id = self.publish_ok()
        self.assertEqual(self.publish.get_ads(ad_id, ["text", "image_hash"]),
                         {"text": VALID_TEXT, "image_hash": self.seeded[0]["hash"]})

    def test_read_path_invalid_field_rejected(self):
        ad_id = self.publish_ok()
        self.assert_error(self.publish.get_ads(ad_id, ["unknown"]), "001", "unknown")

    def test_seeded_image_store_available(self):
        self.assertEqual(self.image_store.list_images(), self.seeded)
        for image in self.seeded:
            self.assertEqual(self.image_store.get_image_by_url(image["url"]), image)
            self.assertEqual(self.image_store.get_image_by_hash(image["hash"]), image)

    def test_invalid_field_001(self):
        self.assert_error(self.publish.publish_ad(self.payload(unknown="oops")),
                          "001", "unknown")

    def test_rejects_second_text_field(self):
        # The base uses one canonical text field; former aliases are unknown.
        self.assert_error(self.publish.publish_ad(self.payload(caption=VALID_TEXT)),
                          "001", "caption")

    def test_invalid_text_value_002(self):
        for text in ("", "  ", "ab", None, 123, self.seeded[0]["url"], self.seeded[0]["hash"]):
            with self.subTest(text=text):
                self.assert_error(self.publish.publish_ad(self.payload(text=text)),
                                  "002", "text")

    def test_invalid_image_value_002(self):
        for image in ("not a url", "https://example.com/a b.jpg", "missing.jpg", "", None):
            with self.subTest(image=image):
                self.assert_error(self.publish.publish_ad(self.payload(image=image)),
                                  "002", "image")

    def test_error_message_shape(self):
        response = self.publish.publish_ad(self.payload(unknown=1))
        self.assertEqual(set(response), {"error_code", "error_message"})
        self.assertTrue(all(isinstance(value, str) for value in response.values()))

    def test_uniqueness(self):
        ids = [self.publish_ok(), self.publish_ok()]
        self.assertNotEqual(*ids)
        for ad_id in ids:
            self.assertEqual(self.publish.get_ads(ad_id)["text"], VALID_TEXT)

    def test_read_returns_none_for_invalid_id(self):
        for ad_id in (999888777, "invalid", None, True):
            with self.subTest(ad_id=ad_id):
                self.assertIsNone(self.publish.get_ads(ad_id))
                self.assertIsNone(self.publish.get_ad(ad_id))

    def test_url_ingestion_persists_new_image(self):
        self.assertIsNone(self.image_store.get_image_by_url(NEW_URL))
        ad_id = self.publish_ok(self.payload(image=NEW_URL))
        expected = hashlib.sha256(NEW_URL.encode()).hexdigest()[:32]
        self.assertEqual(self.publish.get_ads(ad_id)["image_hash"], expected)
        stored = self.fresh_process(
            f"import json, image_store; print(json.dumps(image_store.get_image_by_url({NEW_URL!r})))"
        )
        self.assertEqual(stored["hash"], expected)
        # URL ingestion is metadata-only in the inherited image registry.
        self.assertNotIn("path", stored)

    def test_file_handle_resolves_to_hash(self):
        sample = BASE_DIR / "file_handle_store/sample.jpg"
        expected = hashlib.sha256(sample.read_bytes()).hexdigest()[:32]
        ad_id = self.publish_ok(self.payload(image="sample.jpg"))
        self.assertEqual(self.publish.get_ads(ad_id)["image_hash"], expected)
        image = self.fresh_process(
            f"import json, image_store; print(json.dumps(image_store.get_image_by_hash({expected!r})))"
        )
        self.assertEqual(Path(image["path"]).read_bytes(), sample.read_bytes())
        self.assertTrue(Path(image["path"]).is_relative_to(self.data_dir))

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

    def test_invalid_hash_does_not_ingest_image(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        publisher = self.publishing.AdPublisher(repository, image_storage=storage)
        with self.assertRaises(self.validation.ValidationError) as caught:
            publisher.publish(self.payload(image=NEW_URL, image_hash="bad"))
        self.assertEqual(caught.exception.field, "image_hash")
        storage.store_image.assert_not_called()
        self.assertIsNone(repository.get_ad(1))

    def test_custom_image_storage_and_clock(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
        storage.store_image.return_value = {"hash": "f" * 32}
        publisher = self.publishing.AdPublisher(repository, image_storage=storage,
                                               clock=lambda: 1234567890)
        service = self.object_service(publisher=publisher)
        repository.grant_image(1, "a" * 32, 1)
        # Image ingestion retains precedence over a supplied valid, owned hash.
        ad = service.publish_ad(self.payload(image=NEW_URL, image_hash="a" * 32))
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
        for field in ("text", "creator_id", "image", "access_token"):
            with self.subTest(missing=field):
                data = self.payload()
                del data[field]
                self.assert_error(self.publish.publish_ad(data), "002", field)
        self.assertIsNone(self.publish.get_ad(1))

    def test_hash_validation_boundaries_and_no_ingestion(self):
        repository = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        storage = Mock()
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
        for fields in (123, {"text": True}, True):
            with self.subTest(fields=fields):
                self.assert_error(self.publish.get_ads(ad_id, fields), "002", "fields")
        for field in (None, 1, [], {}):
            with self.subTest(field=field):
                self.assert_error(self.publish.get_ads(ad_id, [field]), "001", field)

    def test_domain_objects_are_immutable(self):
        from dataclasses import FrozenInstanceError

        objects = (self.models.AdAccount(1, "Advertiser"),
                   self.models.AdDraft(1, VALID_TEXT, image_hash="a" * 32),
                   self.object_service().publish_ad(self.payload()))
        for obj in objects:
            with self.subTest(object_type=type(obj).__name__):
                field = "name" if isinstance(obj, self.models.AdAccount) else "text"
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
        import builtins

        original_open = builtins.open
        def fail_registry_write(file, mode="r", *args, **kwargs):
            if str(file) == self.image_store._PERSIST_FILE and "w" in mode:
                raise PermissionError("Registry not writable")
            return original_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=fail_registry_write):
            self.assert_error(self.publish.publish_ad(self.payload(image=NEW_URL)),
                              "002", "image")
        self.assertIsNone(self.publish.get_ad(1))
        self.assertIsNone(self.image_store.get_image_by_url(NEW_URL))

    def test_repository_failure_mapping(self):
        repository = Mock()
        repository.get_account.return_value = self.models.AdAccount(1, "Advertiser")
        repository.get_token_hash.return_value = self.validation.hash_token("token")
        repository.insert_account.side_effect = OSError("DB unavailable")
        repository.save_ad.side_effect = OSError("DB unavailable")
        service = self.service.AdsService(repository)
        data = {"creator_id": 1, "access_token": "token", "text": VALID_TEXT,
                "image_hash": "a" * 32}
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

    def test_file_and_url_ingestion_deduplicate(self):
        for image in (NEW_URL, "sample.jpg"):
            with self.subTest(image=image):
                ids = [self.publish_ok(self.payload(image=image)) for _ in range(2)]
                ads = [self.publish.get_ad(ad_id) for ad_id in ids]
                self.assertEqual(ads[0]["image_hash"], ads[1]["image_hash"])
                matches = [entry for entry in self.image_store.list_images()
                           if entry["hash"] == ads[0]["image_hash"]]
                self.assertEqual(len(matches), 1)

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

    def test_video_fields_cannot_be_reassigned(self):
        from dataclasses import FrozenInstanceError

        video = self.service.AdsService(self.video_repository()).upload_video(self.video_payload())
        self.assertIsInstance(video, self.models.Video)
        for field in ("id", "name", "owner_id", "upload_time", "video_hash"):
            with self.subTest(field=field):
                with self.assertRaises(FrozenInstanceError):
                    setattr(video, field, "Changed")

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

    def test_image_ads_have_no_video(self):
        ad = self.publish.get_ad(self.publish_ok())
        self.assertIsNone(ad["video_id"])
        self.assertEqual(ad["image_hash"], self.seeded[0]["hash"])

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
        stored = self.image_store.get_image_by_hash(expected)
        self.assertEqual(Path(stored["path"]).read_bytes(), b"PNG_BYTES")
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
        self.assertEqual(self.image_store.MAX_IMAGE_BYTES, 30 * 1024 * 1024)
        self.assertEqual(self.video_store.MAX_VIDEO_BYTES, 1024 ** 3)
        at_limit, over_limit = b"12345678", b"123456789"
        with patch.object(self.image_store, "MAX_IMAGE_BYTES", 8):
            fits = self.image_upload(file=self.video_file("fits.png", at_limit))
            self.assertEqual(fits, {"hash": hashlib.sha256(at_limit).hexdigest()[:32]})
            big = self.video_file("big.png", over_limit)
            self.assert_error(self.image_upload(file=big), "002", "file")
            # Publishing with an image path goes through the same limit.
            self.assert_error(self.publish.publish_ad(self.payload(image=big)), "002", "image")
        self.assertEqual(self.stored_image_files(), [f"{fits['hash']}.png"])
        self.assertIsNone(self.publish.get_ad(1))
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
                           "creation_time": creation_time})
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

if __name__ == "__main__":
    unittest.main()
