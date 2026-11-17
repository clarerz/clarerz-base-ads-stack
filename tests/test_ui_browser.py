"""Web UI checks in headless Chromium (Playwright) against a real uvicorn server."""

import hashlib
import json as jsonlib
import re
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

try:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    raise unittest.SkipTest("playwright is not installed; see requirements-dev.txt")

# Imported as a module so its test class is not collected a second time from this file.
import test_api_e2e

PASSWORD = "correct horse"


def call(method, url, json=None, token=None):
    """Return (status, parsed JSON body) for a UI-style request; HTTP errors are returned."""
    headers = {"X-Ads-UI": "1"}
    data = None
    if json is not None:
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


class UiCase(unittest.TestCase):
    """A fresh server and data directory per test, one Chromium per class; defines no tests."""

    start_server = test_api_e2e.ApiEndToEndTests.start_server
    stop_server = staticmethod(test_api_e2e.ApiEndToEndTests.stop_server)
    media_key = None  # Previews need a media key; subclasses that preview set one.
    context_options = {}  # Extra browser-context options, e.g. permissions.

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium is not installed (playwright install chromium): "
                                    f"{exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ads-ui-browser-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        # The default (Secure) cookie: browsers accept it from http://127.0.0.1.
        _, self.url = self.start_server(self.data_dir, media_key=self.media_key)
        self.token = self.create_account(42, "Acme Inc")
        context = self.browser.new_context(**self.context_options)
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.page.set_default_timeout(5000)
        # CSP violations and script errors reach the console; any of them fails the test.
        # Expected API errors (like the 401 for GET /session when signed out) are logged too.
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("console", lambda message: message.type == "error"
                     and not message.text.startswith("Failed to load resource")
                     and self.page_errors.append(message.text))
        self.addCleanup(lambda: self.assertEqual(self.page_errors, []))

    def create_account(self, account_id, name):
        status, body = call("POST", f"{self.url}/accounts", {"id": account_id, "name": name},
                            token=test_api_e2e.ADMIN_KEY)
        self.assertEqual(status, 201, body)
        return body["access_token"]

    def create_login(self, username="acme", account_id=42, token=None):
        status, body = call("POST", f"{self.url}/accounts/{account_id}/users",
                            {"username": username, "password": PASSWORD},
                            token=token or self.token)
        self.assertEqual(status, 201, body)

    def upload_via_api(self, kind, content, **params):
        """Upload with the access token, as setup outside the UI; return the parsed body."""
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}/accounts/42/{kind}?{query}", data=content,
                                     method="POST",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return jsonlib.loads(response.read())

    def end_sessions(self):
        with closing(sqlite3.connect(self.data_dir / "ads.db")) as conn, conn:
            conn.execute("DELETE FROM sessions")

    def publish_via_api(self, text, **media):
        status, body = call("POST", f"{self.url}/accounts/42/ads",
                            {"text": text, **(media or {"image": LINK})}, token=self.token)
        self.assertEqual(status, 201, body)
        return body["id"]

    def media_file(self, name, content):
        path = self.data_dir / name
        path.write_bytes(content)
        return str(path)

    def open(self):
        self.page.goto(f"{self.url}/ui")

    def sign_in(self, login="acme", password=PASSWORD):
        self.open()
        form = self.page.locator("#sign-in-form")
        form.get_by_label("Username or account ID").fill(login)
        form.get_by_label("Password").fill(password)
        form.get_by_role("button", name="Sign in").click()


class SignInTests(UiCase):
    def test_sign_up_sign_out_and_sign_in_by_account_id(self):
        self.open()
        form = self.page.locator("#sign-up-form")
        form.get_by_label("Account ID").fill("42")
        form.get_by_label("Access token").fill(self.token)
        form.get_by_label("Username").fill("acme")
        form.get_by_label("Password").fill(PASSWORD)
        form.get_by_role("button", name="Create login").click()
        expect(self.page.locator("#account-label")).to_have_text(
            "acme · account 42 (Acme Inc)")
        expect(self.page.locator("#signed-out")).to_be_hidden()
        self.page.get_by_role("button", name="Sign out").click()
        expect(self.page.locator("#status")).to_have_text("Signed out.")
        self.sign_in("42")
        expect(self.page.locator("#account-label")).to_have_text(
            "acme · account 42 (Acme Inc)")
        # The session survives a reload.
        self.page.reload()
        expect(self.page.locator("#account-label")).to_have_text(
            "acme · account 42 (Acme Inc)")

    def test_sign_up_errors_name_the_field_and_clear_the_token(self):
        self.open()
        form = self.page.locator("#sign-up-form")
        form.get_by_label("Account ID").fill("42")
        form.get_by_label("Access token").fill("wrong-token")
        form.get_by_label("Username").fill("acme")
        form.get_by_label("Password").fill(PASSWORD)
        form.get_by_role("button", name="Create login").click()
        expect(self.page.locator("#signup-access_token-error")).to_have_text(
            "That access token is not valid for this account.")
        expect(form.get_by_label("Access token")).to_have_value("")
        expect(form.get_by_label("Access token")).to_have_attribute("aria-invalid", "true")

    def test_wrong_password_is_a_login_error_not_an_expired_session(self):
        self.create_login()
        self.sign_in("acme", "wrong password")
        expect(self.page.locator("#signin-error")).to_have_text(
            "Wrong username, account ID, or password.")
        expect(self.page.locator("#status")).to_have_text("")

    def test_keyboard_only_sign_in(self):
        self.create_login()
        self.open()
        self.page.get_by_label("Username or account ID").focus()
        self.page.keyboard.type("acme")
        self.page.keyboard.press("Tab")
        self.page.keyboard.type(PASSWORD)
        self.page.keyboard.press("Enter")
        expect(self.page.locator("#account-label")).to_contain_text("acme")

    def test_account_names_render_as_text(self):
        token = self.create_account(7, "<b>Bold</b> & Co")
        self.create_login("bolder", account_id=7, token=token)
        self.sign_in("bolder")
        expect(self.page.locator("#account-label")).to_have_text(
            "bolder · account 7 (<b>Bold</b> & Co)")
        expect(self.page.locator("#account-label b")).to_have_count(0)


def png(tag=b""):
    """A small PNG header; tag makes distinct content."""
    return test_api_e2e.PNG + tag


class MediaTests(UiCase):
    def setUp(self):
        super().setUp()
        self.create_login()
        self.sign_in()
        expect(self.page.locator("#image-empty")).to_be_visible()

    def upload(self, path, name=""):
        self.page.set_input_files("#upload-file", path)
        self.page.locator("#upload-form").get_by_label("Name").fill(name)
        self.page.get_by_role("button", name="Upload").click()

    def test_upload_image_and_video_selects_them(self):
        self.upload(self.media_file("hero.png", png()), "Summer hero")
        expect(self.page.locator("#status")).to_have_text("Uploaded hero.png.")
        image = self.page.locator("#image-list li")
        expect(image).to_have_count(1)
        expect(image).to_contain_text("Summer hero · ")
        expect(image.get_by_role("radio")).to_be_checked()
        self.upload(self.media_file("clip.mp4", test_api_e2e.MP4), "Promo 15s")
        video = self.page.locator("#video-list li")
        expect(video).to_contain_text("Promo 15s · video #1 · ")
        expect(video.get_by_role("radio")).to_be_checked()
        expect(image.get_by_role("radio")).not_to_be_checked()
        expect(self.page.locator("#upload-progress")).to_be_hidden()

    def test_duplicate_upload_selects_the_existing_image(self):
        self.upload_via_api("images", png(), filename="a.png", image_name="Original")
        self.page.reload()
        expect(self.page.locator("#image-list li")).to_have_count(1)
        self.upload(self.media_file("again.png", png()), "Renamed")
        expect(self.page.locator("#status")).to_have_text(
            "Already uploaded (the new name was not applied).")
        image = self.page.locator("#image-list li")
        expect(image).to_have_count(1)
        expect(image).to_contain_text("Original")
        expect(image.get_by_role("radio")).to_be_checked()

    def test_video_needs_a_name_before_anything_is_sent(self):
        requests = []
        self.page.on("request", lambda request: request.method == "POST"
                     and requests.append(request.url))
        self.upload(self.media_file("clip.mp4", test_api_e2e.MP4))
        expect(self.page.locator("#upload-name-error")).to_have_text("Give the video a name.")
        expect(self.page.locator("#upload-form").get_by_label("Name")).to_have_attribute("aria-invalid", "true")
        self.assertEqual(requests, [])

    def test_name_conflict_is_shown_on_the_name_field(self):
        self.upload_via_api("images", png(b"1"), filename="a.png", image_name="Hero")
        self.upload(self.media_file("other.png", png(b"2")), "Hero")
        expect(self.page.locator("#upload-name-error")).to_have_text(
            "Another of your images already has that name.")

    def test_lists_page_with_load_more(self):
        for index in range(27):
            self.upload_via_api("images", png(bytes([index])), filename="a.png")
        self.page.reload()
        images = self.page.locator("#image-list li")
        expect(images).to_have_count(25)
        self.page.get_by_role("button", name="Load more images").click()
        expect(images).to_have_count(27)
        expect(self.page.get_by_role("button", name="Load more images")).to_be_hidden()

    def test_expired_session_returns_to_sign_in_without_account_data(self):
        self.upload(self.media_file("hero.png", png()), "Hero")
        expect(self.page.locator("#image-list li")).to_have_count(1)
        self.end_sessions()
        self.upload(self.media_file("next.png", png(b"next")), "Next")
        expect(self.page.locator("#status")).to_have_text("Your session expired. Sign in again.")
        expect(self.page.locator("#sign-in-form")).to_be_visible()
        expect(self.page.locator("#image-list li")).to_have_count(0)
        expect(self.page.locator("#account-label")).to_have_text("")


LINK = "https://cdn.example.com/ads/new-hero.png"


class CreateAdTests(UiCase):
    def setUp(self):
        super().setUp()
        self.create_login()
        self.image_hash = self.upload_via_api("images", png(), filename="a.png",
                                              image_name="Hero")["hash"]
        self.video_id = self.upload_via_api("videos", test_api_e2e.MP4, filename="v.mp4",
                                            name="Promo")["id"]
        self.sign_in()
        expect(self.page.locator("#image-list li")).to_have_count(1)
        expect(self.page.locator("#video-list li")).to_have_count(1)

    def publish(self, text):
        form = self.page.locator("#ad-form")
        form.get_by_label("Text").fill(text)
        form.get_by_role("button", name="Publish").click()

    def stored_ad(self, ad_id):
        status, body = call("GET", f"{self.url}/ads/{ad_id}")
        self.assertEqual(status, 200, body)
        return body

    def test_publish_with_an_image_a_video_and_a_link(self):
        self.page.locator("#image-list").get_by_role("radio").check()
        expect(self.page.locator("#ad-media")).to_have_text("Image “Hero”")
        self.publish("Summer styles are here")
        expect(self.page.locator("#status")).to_have_text("Ad #1 created.")
        expect(self.page.locator("#ad-text")).to_have_value("")
        self.page.locator("#video-list").get_by_role("radio").check()
        self.publish("Watch the new collection")
        expect(self.page.locator("#status")).to_have_text("Ad #2 created.")
        self.page.get_by_label("Or an image link").fill(LINK)
        expect(self.page.locator("#ad-media")).to_have_text("Image link")
        self.publish("Fresh picks this week")
        expect(self.page.locator("#status")).to_have_text("Ad #3 created.")
        ads = [self.stored_ad(ad_id) for ad_id in (1, 2, 3)]
        self.assertEqual([(ad["text"], ad["image_hash"], ad["video_id"]) for ad in ads], [
            ("Summer styles are here", self.image_hash, None),
            ("Watch the new collection", None, self.video_id),
            ("Fresh picks this week", hashlib.sha256(LINK.encode()).hexdigest()[:32], None),
        ])

    def test_one_media_source_at_a_time(self):
        image = self.page.locator("#image-list").get_by_role("radio")
        link = self.page.get_by_label("Or an image link")
        image.check()
        link.fill(LINK)
        expect(image).not_to_be_checked()
        image.check()
        expect(link).to_have_value("")

    def test_api_errors_appear_on_their_field(self):
        self.page.locator("#image-list").get_by_role("radio").check()
        self.publish("ab")
        expect(self.page.locator("#ad-text-error")).to_have_text(
            "Use at least 3 characters; the text cannot be only a link or a hash.")
        expect(self.page.locator("#ad-text")).to_have_attribute("aria-invalid", "true")
        self.page.get_by_label("Or an image link").fill("ftp://example.com/a.png")
        self.publish("Valid text here")
        expect(self.page.locator("#ad-image-error")).to_have_text(
            "Enter an http:// or https:// link.")

    def test_media_is_required_before_sending(self):
        requests = []
        self.page.on("request", lambda request: request.method == "POST"
                     and requests.append(request.url))
        self.publish("Valid text here")
        expect(self.page.locator("#ad-error")).to_have_text(
            "Choose an image or video above, or enter an image link.")
        self.assertEqual(requests, [])


class AdListTests(UiCase):
    def setUp(self):
        super().setUp()
        self.create_login()

    def test_ads_page_ten_at_a_time(self):
        for index in range(23):
            self.publish_via_api(f"Ad number {index + 1}")
        self.sign_in()
        rows = self.page.locator("#ad-list li")
        expect(rows).to_have_count(10)
        expect(rows.first).to_contain_text("#23")
        more = self.page.get_by_role("button", name="Load more ads")
        more.click()
        expect(rows).to_have_count(20)
        more.click()
        expect(rows).to_have_count(23)
        expect(rows.last).to_contain_text("#1")
        expect(more).to_be_hidden()

    def test_no_ads_yet(self):
        self.sign_in()
        expect(self.page.locator("#ad-empty")).to_have_text("No ads yet.")
        expect(self.page.locator("#ad-list li")).to_have_count(0)

    def test_rows_show_media_names_and_cut_text_as_text(self):
        image_hash = self.upload_via_api("images", png(), filename="a.png",
                                         image_name="Hero")["hash"]
        video_id = self.upload_via_api("videos", test_api_e2e.MP4, filename="v.mp4",
                                       name="Promo")["id"]
        long_text = "Big savings " + "x" * 130
        self.publish_via_api(long_text, image_hash=image_hash)
        self.publish_via_api("<b>Bold</b> claims", video_id=video_id)
        self.sign_in()
        rows = self.page.locator("#ad-list li")
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text("Video Promo")
        expect(rows.nth(0).locator(".ad-text")).to_have_text("<b>Bold</b> claims")
        expect(rows.nth(0).locator("b")).to_have_count(0)
        expect(rows.nth(1)).to_contain_text("#1")
        expect(rows.nth(1)).to_contain_text("Image Hero")
        text = rows.nth(1).locator(".ad-text")
        expect(text).to_have_text(long_text[:120] + "…")
        expect(text).to_have_attribute("title", long_text)
        expect(rows.nth(1).get_by_role("link", name="#1")).to_have_attribute("href", "/ads/1")

    def test_a_new_ad_is_added_to_the_top(self):
        self.publish_via_api("An older ad")
        self.sign_in()
        expect(self.page.locator("#ad-list li")).to_have_count(1)
        self.page.get_by_label("Or an image link").fill(LINK)
        self.page.locator("#ad-form").get_by_label("Text").fill("Brand new ad")
        self.page.get_by_role("button", name="Publish").click()
        rows = self.page.locator("#ad-list li")
        expect(rows).to_have_count(2)
        expect(rows.first).to_contain_text("#2")
        expect(rows.first).to_contain_text("Brand new ad")


class ShareLinkTests(UiCase):
    media_key = test_api_e2e.MEDIA_KEY
    context_options = {"permissions": ["clipboard-read", "clipboard-write"]}
    TEXT = "Shareable ad text for reviewers"

    def setUp(self):
        super().setUp()
        self.create_login()
        self.publish_via_api(self.TEXT)
        self.sign_in()
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#share-link")).to_be_visible()

    def copy_link(self):
        self.page.get_by_role("button", name="Copy link").click()
        expect(self.page.locator("#share-status")).to_have_text("Link copied")
        return self.page.locator("#share-link").input_value()

    def assert_opens_signed_out(self, link):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(link)
        frames = page.locator("iframe")
        expect(frames).to_have_count(3)
        for index in range(3):
            expect(page.frame_locator("iframe").nth(index).locator(".text")).to_contain_text(
                self.TEXT[:20])

    def test_copy_link_and_open_it_signed_out(self):
        expect(self.page.get_by_label("Share link")).to_have_value(
            re.compile(rf"^{re.escape(self.url)}/accounts/42/ads/1/share\?exp="))
        expect(self.page.locator("#share-expires")).to_contain_text(re.compile("^Expires "))
        link = self.copy_link()
        self.assertEqual(self.page.evaluate("navigator.clipboard.readText()"), link)
        self.assert_opens_signed_out(link)

    def test_share_link_survives_sign_out(self):
        link = self.copy_link()
        self.page.get_by_role("button", name="Close").click()
        self.page.get_by_role("button", name="Sign out").click()
        expect(self.page.locator("#sign-in-form")).to_be_visible()
        self.assert_opens_signed_out(link)

    def test_copy_falls_back_to_selection(self):
        self.page.evaluate(
            "void (navigator.clipboard.writeText = () => Promise.reject(new Error('denied')))")
        self.page.get_by_role("button", name="Copy link").click()
        expect(self.page.locator("#share-status")).to_have_text(
            "Press Ctrl+C (\u2318C on Mac) to copy")
        link = self.page.locator("#share-link").input_value()
        self.assertEqual(self.page.evaluate(
            "[document.activeElement.id, getSelection().toString() || "
            "document.activeElement.value.slice(document.activeElement.selectionStart, "
            "document.activeElement.selectionEnd)]"), ["share-link", link])


class PreviewTests(UiCase):
    media_key = test_api_e2e.MEDIA_KEY

    def setUp(self):
        super().setUp()
        self.create_login()

    def preview_button(self, ad_id):
        return self.page.get_by_role("button", name=f"Preview ad #{ad_id}")

    def test_preview_shows_three_placements(self):
        text = "Fresh deals on everything you need this season, " + "z" * 82
        self.assertEqual(len(text), 130)
        self.publish_via_api(text)
        self.sign_in()
        status = self.page.locator("#status").inner_text()
        self.preview_button(1).click()
        dialog = self.page.locator("#preview-dialog")
        expect(dialog).to_be_visible()
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #1")
        frames = self.page.locator("#preview-frames iframe")
        expect(frames).to_have_count(3)
        # Loading is announced in the dialog, which clears it; the page status is untouched.
        expect(self.page.locator("#preview-message")).to_have_text("")
        expect(self.page.locator("#status")).to_have_text(status)
        expect(dialog.locator("figcaption")).to_have_text(["Feed", "Story", "Banner"])
        expect(frames.first).to_have_attribute("title", "Feed preview of ad #1")
        for index, (width, height) in enumerate((("500", "640"), ("360", "640"), ("600", "354"))):
            expect(frames.nth(index)).to_have_attribute("width", width)
            expect(frames.nth(index)).to_have_attribute("height", height)
        for format in ("feed", "story", "banner"):
            frame = self.page.frame_locator(
                f'#preview-frames figure[data-format="{format}"] iframe')
            expect(frame.locator(".text")).to_contain_text(text[:30])
        expect(dialog.get_by_text("Text is cut off")).to_have_count(3)

    def test_keyboard_open_and_close(self):
        self.publish_via_api("Keyboard friendly ad")
        self.sign_in()
        expect(self.preview_button(1)).to_be_visible()
        for _ in range(60):
            self.page.keyboard.press("Tab")
            if self.page.evaluate(
                    "document.activeElement.getAttribute('aria-label')") == "Preview ad #1":
                break
        else:
            self.fail("Tab never reached the Preview button")
        self.page.keyboard.press("Enter")
        expect(self.page.locator("#preview-dialog")).to_be_visible()
        frames = self.page.locator("#preview-frames iframe")
        expect(frames).to_have_count(3)
        for index in range(3):
            self.assertTrue(frames.nth(index).get_attribute("title"))
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        self.assertEqual(self.page.evaluate("document.activeElement.getAttribute('aria-label')"),
                         "Preview ad #1")

    def test_close_button_closes_the_dialog(self):
        self.publish_via_api("Closable ad")
        self.sign_in()
        self.preview_button(1).click()
        expect(self.page.locator("#preview-dialog")).to_be_visible()
        self.page.get_by_role("button", name="Close").click()
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        expect(self.page.locator("#preview-frames iframe")).to_have_count(0)

    def test_reopening_shows_only_the_new_ad(self):
        self.publish_via_api("First ad")
        self.publish_via_api("Second ad")
        self.sign_in()
        self.preview_button(1).click()
        expect(self.page.locator("#preview-frames iframe")).to_have_count(3)
        self.page.keyboard.press("Escape")
        self.preview_button(2).click()
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #2")
        frames = self.page.locator("#preview-frames iframe")
        expect(frames).to_have_count(3)
        for index in range(3):
            expect(frames.nth(index)).to_have_attribute("src", re.compile(r"/ads/2/"))

    def test_expired_session_from_preview(self):
        self.publish_via_api("Soon orphaned ad")
        self.sign_in()
        expect(self.preview_button(1)).to_be_visible()
        self.end_sessions()
        self.preview_button(1).click()
        expect(self.page.locator("#sign-in-form")).to_be_visible()
        expect(self.page.locator("#status")).to_have_text("Your session expired. Sign in again.")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()


class NoMediaKeyPreviewTests(UiCase):
    def test_no_media_key_says_unavailable(self):
        self.create_login()
        self.publish_via_api("An ad without previews")
        self.sign_in()
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-message")).to_have_text(
            "Previews are unavailable on this server.")
        expect(self.page.locator("#preview-frames iframe")).to_have_count(0)
        expect(self.page.locator("#preview-share")).to_be_hidden()


if __name__ == "__main__":
    unittest.main()
