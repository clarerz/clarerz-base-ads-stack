"""Web logins and sessions: repository, service, and authentication."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_ads_stack import load_isolated_modules


class LoginRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-logins-test-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        load_isolated_modules(self, self.data_dir)
        self.repo = self.repository.SQLiteAdsRepository(self.data_dir / "ads.db")
        for account_id in (1, 2):
            self.repo.insert_account(self.models.AdAccount(account_id, f"Acct {account_id}"),
                                     f"token-hash-{account_id}")

    def user(self, account_id=1, username="acme", token_hash=None):
        return self.models.User(account_id, username, "scrypt$hash",
                                token_hash or f"token-hash-{account_id}")

    def test_v2_database_gains_users_and_sessions(self):
        path = self.data_dir / "v2.db"
        self.repository.SQLiteAdsRepository(path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("DROP TABLE users")
            conn.execute("DROP TABLE sessions")
            conn.execute("PRAGMA user_version = 2")
        self.repository.SQLiteAdsRepository(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"users", "sessions"}, tables)

    def test_create_and_find_user(self):
        self.assertIsNone(self.repo.create_user(self.user(username="Acme")))
        expected = self.models.User(1, "Acme", "scrypt$hash", "token-hash-1", 0, 0)
        self.assertEqual(self.repo.get_user(account_id=1), expected)
        # Usernames match in any case and keep the case they were given.
        self.assertEqual(self.repo.get_user(username="ACME"), expected)
        self.assertIsNone(self.repo.get_user(account_id=2))
        self.assertIsNone(self.repo.get_user(username="nobody"))

    def test_create_user_reports_the_blocking_field(self):
        self.assertEqual(self.repo.create_user(self.user(token_hash="stale")), "access_token")
        self.assertEqual(self.repo.create_user(self.user(account_id=99)), "access_token")
        self.assertIsNone(self.repo.create_user(self.user()))
        self.assertEqual(self.repo.create_user(self.user(username="other")), "account_id")
        self.assertEqual(self.repo.create_user(self.user(account_id=2, username="ACME")),
                         "username")
        self.assertEqual(self.repo.get_user(account_id=1).username, "acme")
        self.assertIsNone(self.repo.get_user(account_id=2))

    def test_token_rotation_frees_the_login_and_ends_its_sessions(self):
        self.repo.create_user(self.user())
        self.repo.create_session("session-1", 1, 2000, 1000)
        self.assertEqual(self.repo.get_session("session-1"), (1, 2000))
        self.repo.set_token_hash(1, "token-hash-new")
        # The stale login's sessions no longer count.
        self.assertIsNone(self.repo.get_session("session-1"))
        # The new token claims the account again; the old username may be reused.
        self.assertIsNone(self.repo.create_user(self.user(token_hash="token-hash-new")))
        self.assertEqual(self.repo.get_user(account_id=1).token_hash, "token-hash-new")
        self.repo.set_token_hash(1, "token-hash-1")
        self.assertIsNone(self.repo.get_session("session-1"))

    def test_record_login_counts_and_resets_failures(self):
        self.repo.create_user(self.user())
        self.repo.record_login(1, False, 100)
        self.repo.record_login(1, False, 105)
        user = self.repo.get_user(account_id=1)
        self.assertEqual((user.failed_logins, user.last_failure), (2, 105))
        self.repo.record_login(1, True, 110)
        user = self.repo.get_user(account_id=1)
        self.assertEqual((user.failed_logins, user.last_failure), (0, 105))

    def test_sessions_store_hashes_and_expire(self):
        self.repo.create_user(self.user())
        self.repo.create_session("old", 1, 1500, 1000)
        self.repo.create_session("current", 1, 9000, 1000)
        # A new session drops expired ones and the one it replaces.
        self.repo.create_session("new", 1, 9000, 1500, replaces="current")
        with closing(sqlite3.connect(self.data_dir / "ads.db")) as conn:
            self.assertEqual(sorted(row[0] for row in conn.execute(
                "SELECT token_hash FROM sessions")), ["new"])
        self.repo.delete_sessions(token_hash="new")
        self.assertIsNone(self.repo.get_session("new"))
        self.repo.create_session("later", 1, 3000, 2000)
        self.repo.delete_sessions(now=3000)
        self.assertIsNone(self.repo.get_session("later"))

    def test_session_without_a_login_is_not_current(self):
        self.repo.create_session("orphan", 2, 9000, 1000)
        self.assertIsNone(self.repo.get_session("orphan"))


PASSWORD = "correct horse"


class ServiceCase(unittest.TestCase):
    """A service with an injected clock and account 42; defines no tests."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-logins-test-")
        self.addCleanup(temporary.cleanup)
        load_isolated_modules(self, Path(temporary.name))
        self.now = [1_800_000_000]
        self.repo = self.repository.SQLiteAdsRepository(self.publish.DB_PATH)
        self.service = self.service.AdsService(self.repo, clock=lambda: self.now[0])
        self.token = self.service.create_account(42, "Acme Inc")[1]

    def sign_up(self, username="acme", password=PASSWORD, account_id=42, token=None):
        return self.service.create_user(account_id, token or self.token, username, password)

    def assert_invalid(self, field, fn, *args):
        with self.assertRaises(self.validation.ValidationError) as caught:
            fn(*args)
        self.assertEqual((caught.exception.code, caught.exception.field), ("002", field))

    def assert_login_fails(self, login, password=PASSWORD):
        self.assert_invalid("login", self.service.login, login, password)


class LoginServiceTests(ServiceCase):
    def test_sign_up_stores_a_salted_scrypt_hash(self):
        user = self.sign_up()
        self.assertEqual((user.account_id, user.username), (42, "acme"))
        self.assertEqual(self.repo.get_user(account_id=42), user)
        other_token = self.service.create_account(7, "Other")[1]
        other = self.sign_up("other", account_id=7, token=other_token)
        for stored in (user.password_hash, other.password_hash):
            self.assertRegex(stored, r"^scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{64}$")
            self.assertNotIn(PASSWORD, stored)
        self.assertNotEqual(user.password_hash, other.password_hash)
        self.assertEqual(user.token_hash, self.validation.hash_token(self.token))

    def test_sign_up_requires_the_current_token(self):
        self.assert_invalid("access_token", self.sign_up, "acme", PASSWORD, 42, "wrong")
        self.assert_invalid("account_id", self.sign_up, "acme", PASSWORD, 99)
        self.assertIsNone(self.repo.get_user(account_id=42))

    def test_username_rules(self):
        for bad in ("12345", "ab", "a" * 33, "acme corp", "acmé", "٤٢abc", "acme\n", None):
            with self.subTest(username=bad):
                self.assert_invalid("username", self.sign_up, bad)
        for good in ("abc", "a" * 32, "acme_corp.v2-1", "7eleven"):
            with self.subTest(username=good):
                self.token = self.service.issue_access_token(42)  # Frees the login.
                self.assertEqual(self.sign_up(good).username, good)

    def test_usernames_are_unique_in_any_case(self):
        self.sign_up("Acme")
        other_token = self.service.create_account(7, "Other")[1]
        self.assert_invalid("username", self.sign_up, "ACME", PASSWORD, 7, other_token)
        # One login per account.
        self.assert_invalid("account_id", self.sign_up, "second")

    def test_password_rules(self):
        for bad in ("a" * 7, "a" * 129, None, 12345678):
            with self.subTest(password=bad):
                self.assert_invalid("password", self.sign_up, "acme", bad)
        self.assert_invalid("password", self.sign_up, "acme", self.token)
        self.assertEqual(self.sign_up("acme", "a" * 8).username, "acme")
        self.token = self.service.issue_access_token(42)
        self.assertEqual(self.sign_up("acme", "a" * 128).username, "acme")

    def test_login_by_username_any_case_or_account_id(self):
        self.sign_up("Acme")
        for login in ("Acme", "aCmE", "42", " 42 "):
            with self.subTest(login=login):
                token, user, account = self.service.login(login, PASSWORD)
                self.assertEqual((user.username, account),
                                 ("Acme", self.models.AdAccount(42, "Acme Inc")))
                self.assertEqual(self.service.get_session(token), (user, account))
        # Non-ASCII digits are a username, not account 42.
        self.assert_login_fails("٤٢")

    def test_every_failure_is_the_same_and_runs_one_scrypt(self):
        self.sign_up()
        self.service.create_account(7, "No login")
        calls = []
        real = self.validation._scrypt

        def counting(*args):
            calls.append(1)
            return real(*args)

        cases = [("nobody", PASSWORD), ("999", PASSWORD), ("7", PASSWORD), ("acme", "wrong pass"),
                 ("acme", None), (None, PASSWORD), ("9" * 30, PASSWORD)]
        with patch.object(self.validation, "_scrypt", counting):
            self.validation.dummy_password_hash()  # Computed once per process, not per login.
            for login, password in cases:
                with self.subTest(login=login, password=password):
                    calls.clear()
                    self.assert_login_fails(login, password)
                    self.assertEqual(len(calls), 1)
            calls.clear()
            self.service.login("acme", PASSWORD)
            self.assertEqual(len(calls), 1)

    def test_repeated_failures_throttle_the_login(self):
        self.sign_up()
        for _ in range(5):
            self.assert_login_fails("acme", "wrong pass")
        self.now[0] += 29
        # Throttled: even the right password fails, and counts as another failure.
        self.assert_login_fails("acme")
        self.now[0] += 29
        self.assert_login_fails("acme")
        self.now[0] += 30
        self.service.login("acme", PASSWORD)
        self.assertEqual(self.repo.get_user(account_id=42).failed_logins, 0)

    def test_success_resets_the_failure_count(self):
        self.sign_up()
        for _ in range(4):
            self.assert_login_fails("acme", "wrong pass")
        self.service.login("acme", PASSWORD)
        for _ in range(4):
            self.assert_login_fails("acme", "wrong pass")
        self.service.login("acme", PASSWORD)

    def test_sessions_last_twelve_hours_and_end_on_logout(self):
        self.sign_up()
        token = self.service.login("acme", PASSWORD)[0]
        self.now[0] += 12 * 3600 - 1
        self.assertIsNotNone(self.service.get_session(token))
        self.now[0] += 1
        self.assertIsNone(self.service.get_session(token))
        token = self.service.login("acme", PASSWORD)[0]
        self.service.logout(token)
        self.assertIsNone(self.service.get_session(token))
        self.assertIsNone(self.service.get_session(None))

    def test_login_ends_the_session_it_replaces(self):
        self.sign_up()
        first = self.service.login("acme", PASSWORD)[0]
        second = self.service.login("acme", PASSWORD, replaces=first)[0]
        self.assertIsNone(self.service.get_session(first))
        self.assertIsNotNone(self.service.get_session(second))

    def test_token_rotation_revokes_the_login(self):
        self.sign_up()
        session = self.service.login("acme", PASSWORD)[0]
        new_token = self.service.issue_access_token(42)
        self.assertIsNone(self.service.get_session(session))
        self.assert_login_fails("acme")
        self.assert_invalid("access_token", self.sign_up, "acme", PASSWORD, 42, self.token)
        self.assertEqual(self.sign_up("acme", "new password", token=new_token).username, "acme")
        self.service.login("acme", "new password")


class SessionAuthenticationTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.sign_up()
        self.session = self.service.login("acme", PASSWORD)[0]
        self.other_token = self.service.create_account(7, "Other")[1]
        video_dir = tempfile.TemporaryDirectory(prefix="ads-logins-video-")
        self.addCleanup(video_dir.cleanup)
        self.video_file = Path(video_dir.name) / "clip.mp4"
        self.video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42 video bytes")
        self.image_file = str(Path(self.publish.__file__).parent / "file_handle_store/sample.jpg")

    def test_a_session_acts_as_the_access_token(self):
        service, token = self.service, self.session
        image_hash = service.upload_image({"owner_id": 42, "access_token": token,
                                           "file": self.image_file})
        self.assertEqual(service.get_image(42, token, image_hash).hash, image_hash)
        self.assertEqual(len(service.list_images(42, token)[0]), 1)
        video = service.upload_video({"owner_id": 42, "access_token": token, "name": "Clip",
                                      "file": str(self.video_file)})
        self.assertEqual(service.get_video(video.id, token), video)
        self.assertEqual(service.rename_video(video.id, "Renamed", token).name, "Renamed")
        self.assertEqual(len(service.list_videos(42, token)[0]), 1)
        ad = service.publish_ad({"creator_id": 42, "access_token": token,
                                 "text": "Fresh picks this week", "image_hash": image_hash})
        self.assertEqual(service.get_ad(ad.id).creator_id, 42)
        self.assertEqual(service.rename_account(42, "Acme Two", token).name, "Acme Two")

    def test_a_session_is_only_good_for_its_own_account(self):
        video = self.service.upload_video({"owner_id": 7, "access_token": self.other_token,
                                           "name": "Theirs", "file": str(self.video_file)})
        attempts = [
            ("access_token", self.service.list_images, 7, self.session),
            ("access_token", self.service.rename_account, 7, "Mine now", self.session),
            ("access_token", self.service.publish_ad,
             {"creator_id": 7, "access_token": self.session, "text": "Not mine",
              "image": "https://cdn.example.com/ads/img1.jpg"}),
            ("video_id", self.service.get_video, video.id, self.session),
        ]
        for field, fn, *args in attempts:
            with self.subTest(fn=fn.__name__):
                self.assert_invalid(field, fn, *args)

    def test_sessions_cannot_sign_up(self):
        self.token = self.service.issue_access_token(42)  # Frees the login.
        self.assert_invalid("access_token", self.sign_up, "acme", PASSWORD, 42, self.session)

    def test_expired_rotated_and_ended_sessions_are_rejected(self):
        list_images = self.service.list_images
        expires = self.now[0] + 12 * 3600
        with patch.object(self.validation.time, "time", return_value=expires - 1):
            self.assertEqual(list_images(42, self.session)[0], [])
        with patch.object(self.validation.time, "time", return_value=expires):
            self.assert_invalid("access_token", list_images, 42, self.session)
        self.service.issue_access_token(42)
        self.assert_invalid("access_token", list_images, 42, self.session)
        self.token = self.service.issue_access_token(42)
        self.sign_up()
        session = self.service.login("acme", PASSWORD)[0]
        self.service.logout(session)
        self.assert_invalid("access_token", list_images, 42, session)

    def test_failed_session_lookup_is_a_storage_error(self):
        with patch.object(self.repo, "get_session", side_effect=OSError("disk")):
            with self.assertRaises(self.validation.StorageError):
                self.service.list_images(42, self.session)


if __name__ == "__main__":
    unittest.main()
