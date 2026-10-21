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
from unittest.mock import patch


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
        for name in ("image_store", "publish"):
            spec = importlib.util.spec_from_file_location(name, BASE_DIR / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            setattr(self, name, module)
        self.seeded = json.loads((BASE_DIR / "images.json").read_text())
        self.assertEqual(self.publish.create_account(1, "Advertiser One"), {"id": 1})

    def payload(self, **changes):
        data = {"creator_id": 1, "text": VALID_TEXT, "image": self.seeded[0]["url"]}
        data.update(changes)
        return data

    def publish_ok(self, data=None):
        response = self.publish.publish_ad(self.payload() if data is None else data)
        self.assertIn("id", response, response)
        self.assertNotIn("error_code", response)
        return response["id"]

    def assert_error(self, response, code, field):
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
        self.assertEqual(self.publish.create_account(2, "Advertiser Two"), {"id": 2})
        ad_id = self.publish_ok(self.payload(creator_id=2))
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

    def test_existing_hash_can_be_published(self):
        data = self.payload()
        del data["image"]
        data["image_hash"] = self.seeded[0]["hash"]
        self.assertEqual(self.publish.get_ads(self.publish_ok(data))["image_hash"], data["image_hash"])


if __name__ == "__main__":
    unittest.main()
