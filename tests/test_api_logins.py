"""HTTP sign-up, login, and session routes through FastAPI's TestClient."""

import html
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("fastapi is not installed; see requirements.txt")

from test_ads_stack import MODULE_NAMES, load_isolated_modules

ADMIN_KEY = "test-admin"
PASSWORD = "correct horse"
UI = {"X-Ads-UI": "1"}
PNG = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (2).to_bytes(4, "big")
       + (3).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
LOGIN_ERROR = {"error_code": "002", "error_message": "The value provided for login is invalid"}


class ApiLoginCase(unittest.TestCase):
    """A TestClient over HTTPS (so Secure cookies round-trip) and account 42; no tests."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-api-logins-")
        self.addCleanup(temporary.cleanup)
        load_isolated_modules(self, Path(temporary.name), MODULE_NAMES + ("api",))
        os.environ.pop("ADS_INSECURE_COOKIES", None)
        self.now = 1_800_000_000
        self.service_obj = self.service.AdsService(
            self.repository.SQLiteAdsRepository(self.publish.DB_PATH), clock=lambda: self.now)
        self.client = self.make_client()
        self.token = self.create_account(42, "Acme Inc")

    def make_client(self, **options):
        app = self.api.create_app(service=self.service_obj, admin_key=ADMIN_KEY, **options)
        return TestClient(app, base_url="https://testserver")

    def create_account(self, account_id, name):
        response = self.client.post("/accounts", json={"id": account_id, "name": name},
                                    headers=self.bearer(ADMIN_KEY))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}", **UI}

    def sign_up(self, username="acme", password=PASSWORD, account_id=42, token=None,
                headers=None):
        return self.client.post(f"/accounts/{account_id}/users",
                                json={"username": username, "password": password},
                                headers=self.bearer(token or self.token) if headers is None
                                else headers)

    def login(self, login="acme", password=PASSWORD, headers=UI, client=None):
        return (client or self.client).post("/session", json={"login": login,
                                                               "password": password},
                                            headers=headers)

    def assert_error(self, response, status, code, field):
        self.assertEqual(response.status_code, status, response.text)
        message = {"001": f"{field} is an invalid field on this endpoint",
                   "003": f"Storage failed while handling {field}"}.get(
                       code, f"The value provided for {field} is invalid")
        self.assertEqual(response.json(), {"error_code": code, "error_message": message})


class SignUpRouteTests(ApiLoginCase):
    def test_sign_up_creates_the_login(self):
        response = self.sign_up()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"username": "acme", "account_id": 42})
        self.assertEqual(self.login().status_code, 200)

    def test_sign_up_errors(self):
        self.assertEqual(self.sign_up().status_code, 201)
        other = self.create_account(7, "Other")
        cases = [
            (self.sign_up(headers={"Authorization": f"Bearer {self.token}"}),
             401, "002", "access_token"),
            (self.sign_up(account_id=7, headers=UI), 401, "002", "access_token"),
            (self.sign_up(account_id=7, token="wrong"), 401, "002", "access_token"),
            (self.sign_up(account_id=99), 400, "002", "account_id"),
            (self.sign_up(account_id=7, token=other, username="12345"), 400, "002", "username"),
            (self.sign_up(account_id=7, token=other, username="ACME"), 400, "002", "username"),
            (self.sign_up(account_id=7, token=other, password="short"), 400, "002", "password"),
            (self.sign_up(account_id=7, token=other, password=other), 400, "002", "password"),
            (self.sign_up(username="second"), 400, "002", "account_id"),
            (self.client.post("/accounts/7/users", json={"username": "x", "extra": 1},
                              headers=self.bearer(other)), 400, "001", "extra"),
        ]
        for index, (response, status, code, field) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_error(response, status, code, field)

    def test_a_session_cannot_sign_up(self):
        self.sign_up()
        session = self.login().cookies["ads_session"]
        self.token = self.service_obj.issue_access_token(42)  # Frees the login.
        self.assert_error(self.sign_up(token=session), 401, "002", "access_token")
        self.assert_error(self.sign_up(headers=UI), 401, "002", "access_token")

    def test_token_rotation_revokes_the_login(self):
        self.sign_up()
        self.assertEqual(self.login().status_code, 200)
        new_token = self.service_obj.issue_access_token(42)
        self.assert_error(self.client.get("/session"), 401, "002", "login")
        self.assertEqual(self.login().json(), LOGIN_ERROR)
        self.assertEqual(self.sign_up(token=new_token).status_code, 201)
        self.assertEqual(self.login().status_code, 200)


class SessionRouteTests(ApiLoginCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.sign_up(username="Acme").status_code, 201)

    def test_login_by_username_any_case_or_account_id(self):
        for login in ("Acme", "aCmE", "42"):
            with self.subTest(login=login):
                response = self.login(login)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"username": "Acme", "account_id": 42,
                                                   "account_name": "Acme Inc"})
                self.assertEqual(response.headers["cache-control"], "no-store")
                cookie = response.headers["set-cookie"]
                self.assertRegex(cookie, r"^ads_session=[A-Za-z0-9_-]{43};")
                for attribute in ("HttpOnly", "Max-Age=43200", "Path=/", "SameSite=strict",
                                  "Secure"):
                    self.assertIn(attribute, cookie)

    def test_every_login_failure_looks_the_same(self):
        self.create_account(7, "No login")
        stale_token = self.create_account(8, "Stale")
        self.assertEqual(self.sign_up("stale", account_id=8, token=stale_token).status_code, 201)
        self.service_obj.issue_access_token(8)
        for login, password in (("nobody", PASSWORD), ("999", PASSWORD), ("7", PASSWORD),
                                ("Acme", "wrong pass"), ("stale", PASSWORD), ("٤٢", PASSWORD)):
            with self.subTest(login=login):
                response = self.login(login, password)
                self.assertEqual((response.status_code, response.json()), (401, LOGIN_ERROR))
                self.assertNotIn("set-cookie", response.headers)

    def test_session_writes_need_the_ui_header(self):
        for response in (self.login(headers={}),
                         self.client.delete("/session")):
            self.assertEqual((response.status_code, response.json()), (401, LOGIN_ERROR))

    def test_insecure_cookies_only_when_opted_out(self):
        client = self.make_client(secure_cookies=False)
        self.assertNotIn("Secure", self.login(client=client).headers["set-cookie"])
        with patch.dict(os.environ, {"ADS_INSECURE_COOKIES": "1"}):
            client = self.make_client()
        self.assertNotIn("Secure", self.login(client=client).headers["set-cookie"])

    def test_session_status_and_logout(self):
        self.assert_error(self.client.get("/session"), 401, "002", "login")
        self.login()
        response = self.client.get("/session")
        self.assertEqual(response.json(), {"username": "Acme", "account_id": 42,
                                           "account_name": "Acme Inc"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        old = self.client.cookies["ads_session"]
        response = self.client.delete("/session", headers=UI)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertRegex(response.headers["set-cookie"], r'^ads_session="";.*Max-Age=0')
        self.client.cookies.set("ads_session", old)
        self.assert_error(self.client.get("/session"), 401, "002", "login")

    def test_login_replaces_the_previous_session(self):
        self.login()
        first = self.client.cookies["ads_session"]
        self.login()
        self.assertNotEqual(self.client.cookies["ads_session"], first)
        self.assertIsNone(self.service_obj.get_session(first))

    def test_storage_failure_is_503(self):
        with patch.object(self.service_obj.repository, "get_user", side_effect=OSError("disk")):
            self.assert_error(self.login(), 503, "003", "login")


class CookieAuthTests(ApiLoginCase):
    """Existing account routes with the session cookie and the UI header."""

    def setUp(self):
        super().setUp()
        self.client = self.make_client(media_key="test-media")
        self.assertEqual(self.sign_up().status_code, 201)
        self.assertEqual(self.login().status_code, 200)
        self.other = self.create_account(7, "Other")

    def upload(self, kind, content, account_id=42, headers=UI, **params):
        return self.client.post(f"/accounts/{account_id}/{kind}", content=content,
                                params=params, headers=headers)

    def test_the_session_cookie_authenticates_account_routes(self):
        response = self.upload("images", PNG, filename="a.png", image_name="Hero")
        self.assertEqual(response.status_code, 201, response.text)
        image_hash = response.json()["hash"]
        images = self.client.get("/accounts/42/images", headers=UI).json()["data"]
        self.assertEqual([image["hash"] for image in images], [image_hash])
        lookup = self.client.get("/accounts/42/images/lookup", params={"image_name": "Hero"},
                                 headers=UI)
        self.assertEqual(lookup.json()["hash"], image_hash)
        response = self.upload("videos", MP4, filename="v.mp4", name="Clip")
        self.assertEqual(response.status_code, 201, response.text)
        video_id = response.json()["id"]
        videos = self.client.get("/accounts/42/videos", headers=UI).json()["data"]
        self.assertEqual([video["id"] for video in videos], [video_id])
        self.assertEqual(self.client.get(f"/videos/{video_id}", headers=UI).json()["name"],
                         "Clip")
        renamed = self.client.patch(f"/videos/{video_id}", json={"name": "Cut"}, headers=UI)
        self.assertEqual(renamed.json()["name"], "Cut")
        ad = self.client.post("/accounts/42/ads", json={"text": "Fresh picks this week",
                                                       "image_hash": image_hash}, headers=UI)
        self.assertEqual(ad.status_code, 201, ad.text)
        account = self.client.patch("/accounts/42", json={"name": "Acme Two"}, headers=UI)
        self.assertEqual(account.json(), {"id": 42, "name": "Acme Two"})

    def test_patch_with_session_cookie(self):
        ad = self.client.post("/accounts/42/ads", json={"text": "Fresh picks this week",
                                                       "image": "https://x.example/a.png"},
                              headers=UI)
        self.assertEqual(ad.status_code, 201, ad.text)
        response = self.client.patch("/accounts/42/ads/1", json={"text": "Fresher picks"},
                                     headers=UI)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["text"], "Fresher picks")
        self.assertEqual(response.json()["update_time"], self.now)
        # The cookie opens only its own account's ads: another account's path or ad.
        self.assertEqual(self.client.post("/accounts/7/ads", json={
            "text": "Other account ad", "image": "https://x.example/b.png"},
            headers={"Authorization": f"Bearer {self.other}"}).status_code, 201)
        response = self.client.patch("/accounts/42/ads/2", json={"text": "Hijacked copy"},
                                     headers=UI)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error_code"], "002")
        self.assertIn("ad_id", response.json()["error_message"])
        response = self.client.patch("/accounts/7/ads/2", json={"text": "Hijacked copy"},
                                     headers=UI)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.client.get("/ads/2").json()["text"], "Other account ad")

    def test_sessions_get_verifiable_signed_urls(self):
        image = self.upload("images", PNG, filename="a.png", image_name="Hero").json()["hash"]
        video = self.upload("videos", MP4, filename="v.mp4", name="Clip").json()["id"]
        ad = self.client.post("/accounts/42/ads", json={"text": "Fresh picks this week",
                                                       "image_hash": image}, headers=UI)
        self.assertEqual(ad.status_code, 201, ad.text)
        get = lambda path, **params: self.client.get(path, params=params, headers=UI).json()
        media = [get("/accounts/42/images")["data"][0], get("/accounts/42/videos")["data"][0],
                 get("/accounts/42/images/lookup", image_name="Hero"), get(f"/videos/{video}")]
        pages = get("/accounts/42/ads/1/previews")["data"]
        media_urls = [item["preview_url"] for item in media]
        page_urls = [item["preview_url"] for item in pages]
        self.assertTrue(page_urls)
        anonymous = TestClient(self.client.app, base_url="https://testserver")
        for url in media_urls + page_urls:
            with self.subTest(url=url):
                self.assertIsInstance(url, str)
                self.assertIn("sig=", url)
                self.assertEqual(anonymous.get(url).status_code, 200)
        self.service_obj.issue_access_token(42)
        for urls, field in ((media_urls, "media"), (page_urls, "preview")):
            for url in urls:
                with self.subTest(revoked=url):
                    self.assert_error(anonymous.get(url), 404, "002", field)

    def test_cookie_fetched_pages_embed_working_urls(self):
        image = self.upload("images", PNG, filename="a.png").json()["hash"]
        ad = self.client.post("/accounts/42/ads", json={"text": "Fresh picks this week",
                                                       "image_hash": image}, headers=UI)
        self.assertEqual(ad.status_code, 201, ad.text)
        page = self.client.get("/accounts/42/ads/1/previews/feed", headers=UI)
        self.assertEqual(page.status_code, 200, page.text)
        src = html.unescape(re.search(r'<img[^>]*\ssrc="([^"]+)"', page.text).group(1))
        anonymous = TestClient(self.client.app, base_url="https://testserver")
        self.assertEqual(anonymous.get(src).status_code, 200)

    def test_cookie_fetched_share_page_frames_verify(self):
        image = self.upload("images", PNG, filename="a.png").json()["hash"]
        ad = self.client.post("/accounts/42/ads", json={"text": "Fresh picks this week",
                                                       "image_hash": image}, headers=UI)
        self.assertEqual(ad.status_code, 201, ad.text)
        page = self.client.get("/accounts/42/ads/1/share", headers=UI)
        self.assertEqual(page.status_code, 200, page.text)
        frames = [html.unescape(src) for src in re.findall(r'<iframe src="([^"]+)"', page.text)]
        self.assertEqual(len(frames), 3)
        anonymous = TestClient(self.client.app, base_url="https://testserver")
        for src in frames:
            with self.subTest(src=src):
                self.assertEqual(anonymous.get(src).status_code, 200)

    def test_the_cookie_only_counts_for_its_own_account(self):
        video = self.client.post("/accounts/7/videos", content=MP4,
                                 params={"filename": "v.mp4", "name": "Theirs"},
                                 headers=self.bearer(self.other)).json()["id"]
        cases = [
            (self.client.get("/accounts/7/images", headers=UI), 401, "access_token"),
            (self.upload("images", PNG, account_id=7, filename="a.png"), 401, "access_token"),
            (self.client.post("/accounts/7/ads", json={"text": "Not mine", "video_id": video},
                              headers=UI), 401, "access_token"),
            (self.client.get(f"/videos/{video}", headers=UI), 404, "video_id"),
            (self.client.patch(f"/videos/{video}", json={"name": "Mine"}, headers=UI),
             404, "video_id"),
        ]
        for index, (response, status, field) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_error(response, status, "002", field)

    def test_the_cookie_needs_the_ui_header_and_no_authorization(self):
        cases = [
            self.client.get("/accounts/42/images"),
            self.client.get("/accounts/42/images", headers={"X-Ads-UI": "true"}),
            self.client.get("/accounts/42/images", headers=self.bearer("wrong")),
            self.client.get("/accounts/42/images", headers={"Authorization": "Basic eDp5",
                                                            **UI}),
            self.upload("images", PNG, headers={}, filename="a.png"),
        ]
        for index, response in enumerate(cases):
            with self.subTest(case=index):
                self.assert_error(response, 401, "002", "access_token")

    def test_the_cookie_never_acts_as_the_admin_key(self):
        response = self.client.post("/accounts", json={"id": 9, "name": "Sneaky"}, headers=UI)
        self.assert_error(response, 401, "002", "access_token")

    def test_bearer_callers_still_get_signed_urls(self):
        self.upload("images", PNG, filename="a.png")
        response = self.client.get("/accounts/42/images", headers=self.bearer(self.token))
        self.assertIn("sig=", response.json()["data"][0]["preview_url"])


class UiRouteTests(ApiLoginCase):
    def test_ui_files_are_served_with_a_strict_policy(self):
        for path, content_type in (("/ui", "text/html"), ("/ui/app.js", "text/javascript"),
                                   ("/ui/app.css", "text/css"),
                                   ("/ui/preview.css", "text/css")):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith(content_type))
                self.assertEqual(response.headers["content-security-policy"],
                                 "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "connect-src 'self'; img-src 'self' http: https:; media-src 'self'; "
                                 "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assert_error(self.client.get("/ui/index.html"), 404, "002", "path")
        self.assert_error(self.client.get("/ui/..%2Fapi.py"), 404, "002", "path")

    def test_ui_source_has_no_inline_code_or_html_insertion(self):
        web = Path(self.api.WEB_DIR)
        page = (web / "index.html").read_text()
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(page, r"<style|\sstyle=")
        script = (web / "app.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(sink, script)


if __name__ == "__main__":
    unittest.main()
