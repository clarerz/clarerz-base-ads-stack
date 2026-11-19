"""Web UI checks in headless Chromium (Playwright) against a real uvicorn server."""

import base64
import hashlib
import json as jsonlib
import re
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest
import urllib.parse
import urllib.request
import zlib

try:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    raise unittest.SkipTest("playwright is not installed; see requirements-dev.txt")

from helpers import request
# Imported as a module so its test class is not collected a second time from this file.
import test_api_e2e

PASSWORD = "correct horse"

# Records about 1 s of a changing canvas as VP8 WebM and returns it base64-encoded.
RECORD_WEBM = """async () => {
    const canvas = Object.assign(document.createElement("canvas"), {width: 320, height: 180});
    const context = canvas.getContext("2d");
    const recorder = new MediaRecorder(canvas.captureStream(30), {mimeType: "video/webm;codecs=vp8"});
    const chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    const stopped = new Promise((resolve) => { recorder.onstop = resolve; });
    recorder.start();
    for (let frame = 0; frame < 30; frame++) {
        context.fillStyle = `hsl(${frame * 12}, 80%, 50%)`;
        context.fillRect(0, 0, 320, 180);
        await new Promise((resolve) => setTimeout(resolve, 33));
    }
    recorder.stop();
    await stopped;
    const bytes = new Uint8Array(await new Blob(chunks).arrayBuffer());
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    return btoa(binary);
}"""


def call(method, url, json=None, token=None):
    """Return (status, parsed JSON body) for a UI-style request; HTTP errors are returned."""
    return request(method, url, json=json, token=token, headers={"X-Ads-UI": "1"})


class UiCase(unittest.TestCase):
    """One server and Chromium per class, data emptied before each test; defines no tests."""

    media_key = None  # Previews need a media key; subclasses that preview set one.
    context_options = {}  # Extra browser-context options, e.g. permissions.
    _webm = None

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium is not installed (playwright install chromium): "
                                    f"{exc}")
        cls.data_dir = Path(tempfile.mkdtemp(prefix="ads-ui-browser-"))
        cls.addClassCleanup(shutil.rmtree, cls.data_dir)
        # The default (Secure) cookie: browsers accept it from http://127.0.0.1.
        cls.server, cls.url = test_api_e2e.launch_server(cls.data_dir, cls.addClassCleanup,
                                                         media_key=cls.media_key)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.assertIsNone(self.server.poll(), "the class's server exited")
        self.empty_data()
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

    def empty_data(self):
        """Return the server to a fresh start: no rows (IDs restart at 1) and no stored files."""
        with closing(sqlite3.connect(self.data_dir / "ads.db")) as conn, conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            for (table,) in tables:
                conn.execute(f'DELETE FROM "{table}"')
        # Directories stay: the server makes some only at startup.
        for path in self.data_dir.iterdir():
            if path.is_dir():
                for child in path.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            elif not path.name.startswith("ads.db") and path.name != "image_registry.lock":
                path.unlink()

    @classmethod
    def webm(cls):
        """A real, playable VP8 WebM of about 1 s. Playwright's Chromium lacks H.264, so the
        MP4 fixtures can't play."""
        if UiCase._webm is None:
            page = cls.browser.new_page()
            try:
                UiCase._webm = base64.b64decode(page.evaluate(RECORD_WEBM))
            finally:
                page.close()
        return UiCase._webm

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

    def wait_for(self, held):
        for _ in range(50):
            if held:
                return held[-1]
            self.page.wait_for_timeout(100)
        self.fail("No request was held")

    def click_through(self, locator):
        """Click where the element is, even when it is inert and the click falls through."""
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

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


def real_png(tag=b""):
    """A decodable 1x1 PNG; tag, after IEND where decoders ignore it, makes distinct content."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"") + tag)


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

    def test_share_story_controls_reachable(self):
        video_id = self.upload_via_api("videos", test_api_e2e.MP4, filename="v.mp4",
                                       name="Clip")["id"]
        ad_id = self.publish_via_api("A video ad", video_id=video_id)
        link = call("GET", f"{self.url}/accounts/42/ads/{ad_id}/previews",
                    token=self.token)[1]["share_url"]
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(link)
        story = page.frame_locator("iframe").nth(1)
        # The controls bar is the bottom strip of the 360x640 video; the text block must not catch it.
        self.assertEqual(story.locator("body").evaluate(
            "() => document.elementFromPoint(30, 625).tagName"), "VIDEO")

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
        ads = self.page.locator("#preview-frames article.ad")
        expect(ads).to_have_count(3)
        # Loading is announced in the dialog, which clears it; the page status is untouched.
        expect(self.page.locator("#preview-message")).to_have_text("")
        expect(self.page.locator("#status")).to_have_text(status)
        expect(dialog.locator("figcaption")).to_have_text(["Feed", "Story", "Banner"])
        expect(ads.locator(".text")).to_have_text([
            text[:125] + "… See more", text[:125] + "…", text[:40].rstrip() + "…"])
        expect(ads.locator(".more")).to_have_count(1)
        expect(self.page.locator("#preview-frames iframe")).to_have_count(0)
        expect(dialog.get_by_text("Text is cut off")).to_have_count(3)

    def test_placements_match_preview_pages(self):
        text = "<b>Sale</b> " + "🎉" * 40  # Markup stays literal; the emoji cut matches the server.
        self.publish_via_api(text)
        self.sign_in()
        self.preview_button(1).click()
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)
        expect(self.page.locator("#preview-frames b")).to_have_count(0)
        status, listing = call("GET", f"{self.url}/accounts/42/ads/1/previews", token=self.token)
        self.assertEqual(status, 200, listing)
        GEOMETRY = """(root) => { const r = root.getBoundingClientRect(); const box = (sel) => {
            const e = root.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect();
            return [b.x - r.x, b.y - r.y, b.width, b.height, getComputedStyle(e).font]; };
            return {ad: [r.width, r.height], header: box("header"), text: box(".text"),
                    media: box(".media"), shown: root.querySelector(".text").textContent}; }"""
        for entry in listing["data"]:
            with self.subTest(format=entry["format"]):
                server = self.browser.new_page()
                self.addCleanup(server.close)
                server.goto(entry["preview_url"])
                expected = server.locator("article.ad").evaluate(GEOMETRY)
                actual = self.page.locator(
                    f'figure[data-format="{entry["format"]}"] article.ad').evaluate(GEOMETRY)
                self.assertEqual(actual, expected)

    def test_video_playback_matches_share_pages(self):
        video_id = self.upload_via_api("videos", self.webm(), filename="clip.webm",
                                       name="Clip")["id"]
        self.publish_via_api("Watch this clip", video_id=video_id)
        self.page.set_viewport_size({"width": 1280, "height": 1000})  # Story fits on screen.
        self.sign_in()
        self.preview_button(1).click()
        videos = self.page.locator("#preview-frames video")
        expect(videos).to_have_count(3)
        for index in range(3):
            expect(videos.nth(index)).to_have_attribute("controls", "")
            expect(videos.nth(index)).to_have_js_property("muted", True)
        story = self.page.locator('figure[data-format="story"] video')
        expect(story).to_have_attribute("autoplay", "")
        expect(story).to_have_attribute("loop", "")
        expect(story).to_have_js_property("paused", False)
        feed = self.page.locator('figure[data-format="feed"] video')
        expect(feed).not_to_have_attribute("autoplay", "")
        expect(feed).to_have_js_property("readyState", 4)
        expect(feed).to_have_js_property("paused", True)
        box = feed.bounding_box()  # A click on a video with controls toggles playback.
        self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        expect(feed).to_have_js_property("paused", False)
        # Story's text block covers the lower video but passes clicks to its controls bar.
        article = self.page.locator('figure[data-format="story"] article.ad')
        article.scroll_into_view_if_needed()
        self.assertEqual(article.evaluate("""(root) => { const r = root.getBoundingClientRect();
            return document.elementFromPoint(r.x + 30, r.y + 625).tagName; }"""), "VIDEO")

    def test_broken_media_retries_once(self):
        image_hash = self.upload_via_api("images", png(), filename="a.png")["hash"]
        self.publish_via_api("Broken media ad", image_hash=image_hash)
        self.page.route("**/media/**", lambda route: route.fulfill(status=404))
        listings = []
        self.page.on("request", lambda request: urllib.parse.urlsplit(request.url).path
                     == "/accounts/42/ads/1/previews" and listings.append(request.url))
        self.sign_in()
        self.preview_button(1).click()
        boxes = self.page.locator("#preview-frames .media")
        expect(boxes).to_have_text(["Media unavailable"] * 3)
        expect(self.page.locator("#preview-frames img")).to_have_count(0)
        self.assertEqual(len(listings), 2)

    def test_preview_pages_keep_their_geometry(self):
        image_hash = self.upload_via_api("images", png(), filename="a.png")["hash"]
        ad_id = self.publish_via_api("Geometry check", image_hash=image_hash)
        status, body = call("GET", f"{self.url}/accounts/42/ads/{ad_id}/previews", token=self.token)
        self.assertEqual(status, 200, body)
        urls = {entry["format"]: entry["preview_url"] for entry in body["data"]}
        # (ad size, media size, text height or None to check the story text's bottom edge)
        expected = {"feed": ((500, 640), (500, 500), 96), "story": ((360, 640), (360, 640), None),
                    "banner": ((600, 354), (600, 314), 40)}
        for name, (ad_size, media_size, text_height) in expected.items():
            with self.subTest(format=name):
                self.page.goto(urls[name])
                boxes = {part: self.page.locator(part).bounding_box()
                         for part in (".ad", ".media", ".text")}
                self.assertEqual((boxes[".ad"]["width"], boxes[".ad"]["height"]), ad_size)
                self.assertEqual((boxes[".media"]["width"], boxes[".media"]["height"]), media_size)
                if text_height is None:
                    self.assertEqual(boxes[".text"]["y"] + boxes[".text"]["height"], 640)
                else:
                    self.assertEqual(boxes[".text"]["height"], text_height)

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
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)
        expect(self.page.locator("#preview-frames figcaption")).to_have_text(
            ["Feed", "Story", "Banner"])
        expect(self.page.locator("#preview-frames iframe")).to_have_count(0)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        self.assertEqual(self.page.evaluate("document.activeElement.getAttribute('aria-label')"),
                         "Preview ad #1")

    def test_close_button_closes_the_dialog(self):
        self.publish_via_api("Closable ad")
        self.sign_in()
        self.preview_button(1).click()
        expect(self.page.locator("#preview-dialog")).to_be_visible()
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)
        self.page.get_by_role("button", name="Close").click()
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        expect(self.page.locator("#preview-frames > *")).to_have_count(0)

    def test_reopening_shows_only_the_new_ad(self):
        self.publish_via_api("First ad")
        self.publish_via_api("Second ad")
        self.sign_in()
        self.preview_button(1).click()
        expect(self.page.locator("#preview-frames .text")).to_have_text(["First ad"] * 3)
        self.page.keyboard.press("Escape")
        self.preview_button(2).click()
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #2")
        expect(self.page.locator("#preview-frames .text")).to_have_text(["Second ad"] * 3)
        expect(self.page.locator("#preview-frames iframe")).to_have_count(0)

    def test_expired_session_from_preview(self):
        self.publish_via_api("Soon orphaned ad")
        self.sign_in()
        expect(self.preview_button(1)).to_be_visible()
        self.end_sessions()
        self.preview_button(1).click()
        expect(self.page.locator("#sign-in-form")).to_be_visible()
        expect(self.page.locator("#status")).to_have_text("Your session expired. Sign in again.")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()


WARNING_COPY = {"text_truncated": "Text is cut off", "media_cropped": "Image is cropped to fit"}
FORMATS = ("feed", "story", "banner")


class TextEditTests(UiCase):
    media_key = test_api_e2e.MEDIA_KEY
    TEXT = "Fresh deals on everything you need this season, " + "z" * 82
    NEW = "Brand new words for this ad"

    def setUp(self):
        super().setUp()
        self.create_login()
        image_hash = self.upload_via_api("images", png(), filename="a.png")["hash"]
        self.publish_via_api(self.TEXT, image_hash=image_hash)
        self.publish_via_api("Second ad text", image_hash=image_hash)
        self.patches = []
        self.page.on("request", lambda request: request.method == "PATCH"
                     and self.patches.append(request))
        self.sign_in()
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)

    def target(self, ad_format):
        return self.page.locator(f'#preview-frames figure[data-format="{ad_format}"] .text-body')

    def box(self):
        return self.page.locator("#preview-frames textarea")

    def click_out(self):
        self.page.locator("#preview-heading").click()

    def stored_text(self, ad_id=1):
        status, body = call("GET", f"{self.url}/ads/{ad_id}")
        self.assertEqual(status, 200, body)
        return body["text"]

    def hold_patches(self):
        """Hold every PATCH until the test continues it; return the held routes."""
        held = []
        self.page.route("**/accounts/42/ads/*", lambda route: held.append(route)
                        if route.request.method == "PATCH" else route.continue_())
        return held

    def test_text_outline_only_on_hover_or_focus(self):
        style = "(e) => getComputedStyle(e).outlineStyle"
        for ad_format in FORMATS:
            with self.subTest(format=ad_format):
                target = self.target(ad_format)
                self.page.mouse.move(0, 0)
                self.assertEqual(target.evaluate(style), "none")
                target.hover()
                self.assertNotEqual(target.evaluate(style), "none")
                self.page.mouse.move(0, 0)
                target.focus()
                self.assertNotEqual(target.evaluate(style), "none")
                target.blur()
        # In story, the outline is the text's own box, not the padded overlay block's.
        story = self.target("story")
        story.hover()
        block = self.page.locator('#preview-frames figure[data-format="story"] .text')
        self.assertEqual(block.evaluate(style), "none")
        inner, outer = story.bounding_box(), block.bounding_box()
        self.assertLess(inner["height"], outer["height"] - 170)
        self.assertGreater(inner["y"], outer["y"] + 40)

    def test_click_edit_blur_saves(self):
        self.target("feed").click()
        box = self.box()
        expect(box).to_be_focused()
        expect(box).to_have_value(self.TEXT)
        held = self.hold_patches()
        box.fill(self.NEW)
        self.click_out()
        route = self.wait_for(held)
        expect(box).to_have_attribute("readonly", "")
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(self.patches[0].post_data_json, {"text": self.NEW})
        route.continue_()
        expect(self.page.locator("#preview-frames .text")).to_have_text([self.NEW] * 3)
        expect(box).to_have_count(0)
        status, listing = call("GET", f"{self.url}/accounts/42/ads/1/previews", token=self.token)
        self.assertEqual(status, 200, listing)
        for entry in listing["data"]:
            figure = self.page.locator(f'#preview-frames figure[data-format="{entry["format"]}"]')
            expect(figure.locator(".text-body")).to_have_text(entry["text"])
            expect(figure.locator(".warning")).to_have_text(
                [WARNING_COPY[warning] for warning in entry["warnings"]])
        expect(self.page.locator("#ad-1 .ad-text")).to_have_text(self.NEW)
        self.assertEqual(self.stored_text(), self.NEW)
        self.assertEqual(len(self.patches), 1)

    def test_focus_returns_to_opener_after_save(self):
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.click_out()
        expect(self.page.locator("#ad-1 .ad-text")).to_have_text(self.NEW)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        expect(self.page.get_by_role("button", name="Preview ad #1")).to_be_focused()

    def test_later_save_clears_listing_error(self):
        listings = []

        def first_fails(route):
            listings.append(route)
            if len(listings) > 1:
                return route.continue_()
            route.fulfill(status=503, content_type="application/json", body=jsonlib.dumps(
                {"error_code": "003", "error_message": "Storage is unavailable"}))

        self.page.route("**/accounts/42/ads/1/previews", first_fails)
        message = self.page.locator("#preview-message")
        for text in (self.NEW, "Second set of new words"):
            self.target("feed").click()
            self.box().fill(text)
            self.click_out()
            expect(self.page.locator("#preview-frames .text-body")).to_have_text([text] * 3)
            if len(listings) == 1:
                expect(message).to_have_text("Storage is unavailable")
        expect(message).to_have_text("")

    def test_unchanged_text_sends_nothing(self):
        self.target("feed").click()
        expect(self.box()).to_be_focused()
        self.click_out()
        expect(self.box()).to_have_count(0)
        expect(self.target("feed")).to_have_text(self.TEXT[:125] + "…")
        self.assertEqual(self.patches, [])

    def test_unchanged_crlf_text_sends_nothing(self):
        self.publish_via_api("Line one\r\nLine two\rLine three")
        self.page.keyboard.press("Escape")
        self.page.reload()
        self.page.get_by_role("button", name="Preview ad #3").click()
        self.target("feed").click()
        expect(self.box()).to_have_value("Line one\nLine two\nLine three")  # Textareas use \n.
        self.click_out()
        expect(self.box()).to_have_count(0)
        self.assertEqual(self.patches, [])
        self.assertEqual(self.stored_text(3), "Line one\r\nLine two\rLine three")

    def test_escape_cancels(self):
        self.target("feed").click()
        self.box().fill("Something else entirely")
        self.page.keyboard.press("Escape")
        expect(self.box()).to_have_count(0)
        expect(self.page.locator("#preview-dialog")).to_be_visible()
        expect(self.page.locator("#preview-frames .text").first).to_have_text(
            self.TEXT[:125] + "… See more")
        expect(self.target("feed")).to_be_focused()
        self.assertEqual(self.patches, [])
        self.assertEqual(self.stored_text(), self.TEXT)

    def test_window_blur_does_not_save(self):
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.page.evaluate("""() => {
            document.hasFocus = () => false;
            window.dispatchEvent(new Event("blur"));
            document.querySelector("#preview-frames textarea").dispatchEvent(new Event("blur"));
        }""")
        expect(self.box()).to_have_value(self.NEW)
        expect(self.box()).not_to_have_attribute("readonly", "")
        self.assertEqual(self.patches, [])

    def test_invalid_text_stays_open(self):
        self.target("feed").click()
        self.box().fill("ab")
        self.click_out()
        error = self.page.locator('#preview-frames figure[data-format="feed"] .error')
        expect(error).to_have_text(
            "Use at least 3 characters; the text cannot be only a link or a hash.")
        box = self.box()
        expect(box).to_have_value("ab")
        expect(box).not_to_have_attribute("readonly", "")
        expect(box).to_have_attribute("aria-invalid", "true")
        self.assertIn(error.get_attribute("id"), box.get_attribute("aria-describedby").split())
        self.assertEqual(self.stored_text(), self.TEXT)

    def test_one_edit_at_a_time(self):
        held = self.hold_patches()
        self.target("feed").click()
        self.box().fill(self.NEW)
        expect(self.target("story")).to_have_js_property("inert", True)
        self.click_out()
        route = self.wait_for(held)
        self.click_through(self.target("story"))
        expect(self.box()).to_have_count(1)
        self.assertEqual(len(self.patches), 1)
        route.continue_()
        expect(self.page.locator("#preview-frames .text-body")).to_have_text([self.NEW] * 3)
        self.page.unroute("**/accounts/42/ads/*")
        # After a rejected save, the other targets stay inert until Escape.
        self.target("feed").click()
        self.box().fill("ab")
        self.click_out()
        expect(self.page.locator("#preview-frames .error")).to_have_count(1)
        self.click_through(self.target("story"))
        expect(self.page.locator('figure[data-format="story"] textarea')).to_have_count(0)
        expect(self.target("story")).to_have_js_property("inert", True)
        self.box().focus()
        self.page.keyboard.press("Escape")
        expect(self.target("story")).to_have_js_property("inert", False)
        self.target("story").click()
        expect(self.page.locator('figure[data-format="story"] textarea')).to_be_focused()
        self.assertEqual(len(self.patches), 2)

    def test_media_retry_keeps_open_box(self):
        self.publish_via_api("A link image ad", image=LINK)
        self.page.route("https://cdn.example.com/**", lambda route: route.fulfill(status=404))
        listings = []
        held = []
        self.page.route("**/accounts/42/ads/3/previews", lambda route: (
            listings.append(route), held.append(route) if len(listings) == 2
            else route.continue_()))
        self.page.keyboard.press("Escape")
        self.page.reload()
        self.page.get_by_role("button", name="Preview ad #3").click()
        retry = self.wait_for(held)  # The image failed; its listing re-fetch is held.
        self.target("feed").click()
        self.box().fill(self.NEW)
        retry.continue_()
        expect(self.page.locator("#preview-frames .media")).to_have_text(
            ["Media unavailable"] * 3)
        expect(self.box()).to_have_value(self.NEW)
        expect(self.box()).to_be_focused()
        self.click_out()
        expect(self.page.locator("#preview-frames .text")).to_have_text([self.NEW] * 3)
        self.assertEqual(self.stored_text(3), self.NEW)

    def test_failed_save_without_busy_element(self):
        # setBusy's element is optional; without one, the error goes to the dialog's message line.
        result = self.page.evaluate("""async () => {
            setBusy(true);
            const inert = document.querySelector("#preview-frames .text-body").inert;
            const ad = await runEdit(1, {text: "ab"}, "text");
            setBusy(false);
            return [inert, ad, editor.busy,
                    document.querySelector("#preview-frames .text-body").inert];
        }""")
        self.assertEqual(result, [True, None, False, False])
        expect(self.page.locator("#preview-message")).to_have_text(
            "Use at least 3 characters; the text cannot be only a link or a hash.")
        self.assertEqual(self.stored_text(), self.TEXT)

    def test_close_saves_pending_text(self):
        held = self.hold_patches()
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.page.get_by_role("button", name="Close").click()
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        self.wait_for(held).continue_()
        expect(self.page.locator("#ad-1 .ad-text")).to_have_text(self.NEW)
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(self.stored_text(), self.NEW)

    def test_close_reports_failure_in_status(self):
        self.target("feed").click()
        self.box().fill("ab")
        self.page.get_by_role("button", name="Close").click()
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        expect(self.page.locator("#status")).to_have_text(
            "Ad #1: text not saved: The value provided for text is invalid")
        self.assertEqual(self.stored_text(), self.TEXT)

    def test_late_response_does_not_touch_new_dialog(self):
        held = self.hold_patches()
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.page.get_by_role("button", name="Close").click()
        route = self.wait_for(held)
        self.page.get_by_role("button", name="Preview ad #2").click()
        texts = self.page.locator("#preview-frames .text")
        expect(texts).to_have_text(["Second ad text"] * 3)
        # A re-render would drop this mark.
        self.page.locator("#preview-frames figure").evaluate_all(
            "(figures) => figures.forEach((figure) => { figure.dataset.mark = 'kept'; })")
        route.continue_()
        expect(self.page.locator("#ad-1 .ad-text")).to_have_text(self.NEW)
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #2")
        expect(texts).to_have_text(["Second ad text"] * 3)
        expect(self.page.locator('#preview-frames figure[data-mark="kept"]')).to_have_count(3)

    def test_reopening_waits_for_pending_save(self):
        held = self.hold_patches()
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.page.get_by_role("button", name="Close").click()
        route = self.wait_for(held)
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-message")).to_have_text("Loading preview…")
        expect(self.page.locator("#preview-frames > *")).to_have_count(0)
        route.continue_()
        texts = self.page.locator("#preview-frames .text-body")
        expect(texts).to_have_text([self.NEW] * 3)
        self.page.unroute("**/accounts/42/ads/*")
        self.target("feed").click()
        expect(self.box()).to_have_value(self.NEW)
        self.box().press("End")
        self.box().type(" tweak")
        self.click_out()
        expect(texts).to_have_text([self.NEW + " tweak"] * 3)
        self.assertEqual(self.stored_text(), self.NEW + " tweak")

    def test_waiting_preview_gives_way_to_another_ad(self):
        held = self.hold_patches()
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.page.get_by_role("button", name="Close").click()
        route = self.wait_for(held)
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-message")).to_have_text("Loading preview…")
        self.page.keyboard.press("Escape")
        self.page.get_by_role("button", name="Preview ad #2").click()
        texts = self.page.locator("#preview-frames .text")
        expect(texts).to_have_text(["Second ad text"] * 3)
        # A re-render would drop this mark.
        self.page.locator("#preview-frames figure").evaluate_all(
            "(figures) => figures.forEach((figure) => { figure.dataset.mark = 'kept'; })")
        route.continue_()
        expect(self.page.locator("#ad-1 .ad-text")).to_have_text(self.NEW)
        self.page.wait_for_timeout(300)
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #2")
        expect(texts).to_have_text(["Second ad text"] * 3)
        expect(self.page.locator('#preview-frames figure[data-mark="kept"]')).to_have_count(3)

    def test_expired_session_during_text_save(self):
        self.target("feed").click()
        self.box().fill(self.NEW)
        self.end_sessions()
        self.click_out()
        expect(self.page.locator("#sign-in-form")).to_be_visible()
        expect(self.page.locator("#status")).to_have_text("Your session expired. Sign in again.")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        expect(self.page.locator("#preview-frames > *")).to_have_count(0)
        self.assertEqual(self.stored_text(), self.TEXT)

    def test_keyboard_editing(self):
        reached = set()
        for _ in range(40):
            self.page.keyboard.press("Tab")
            reached.add(self.page.evaluate("""() => document.activeElement.matches('.text-body')
                ? document.activeElement.closest('figure').dataset.format : null"""))
        self.assertLessEqual(set(FORMATS), reached)
        status, listing = call("GET", f"{self.url}/accounts/42/ads/1/previews", token=self.token)
        self.assertEqual(status, 200, listing)
        for entry in listing["data"]:
            with self.subTest(format=entry["format"]):
                figure = self.page.locator(f'#preview-frames figure[data-format="{entry["format"]}"]')
                target = figure.get_by_role("button", name=entry["text"], exact=True)
                expect(target).to_have_count(1)
                expect(target).to_have_accessible_description("Edit ad text")
                self.assertEqual(target.evaluate(
                    "(e) => document.getElementById(e.getAttribute('aria-describedby')).textContent"),
                    "Edit ad text")
                for key in ("Enter", " "):
                    figure.locator(".text-body").focus()
                    self.page.keyboard.press(key)
                    expect(figure.locator("textarea")).to_be_focused()
                    expect(figure.locator("textarea")).to_have_value(self.TEXT)
                    self.page.keyboard.press("Escape")
                    expect(figure.locator(".text-body")).to_be_focused()
        self.assertEqual(self.patches, [])


class MediaReplaceTests(UiCase):
    media_key = test_api_e2e.MEDIA_KEY
    IMAGES = ".jpg,.jpeg,.png,.gif,.webp"

    def setUp(self):
        super().setUp()
        self.create_login()
        self.old_hash = self.upload_via_api("images", real_png(), filename="a.png")["hash"]
        self.publish_via_api("An ad with a replaceable image", image_hash=self.old_hash)
        self.sent = []  # Uploads and updates, as (method, path, query).
        self.page.on("request", lambda request: request.method in ("POST", "PATCH")
                     and self.sent.append((request.method, *urllib.parse.urlsplit(request.url)[2:4])))
        # Playwright turns chooser interception on and off, without waiting, as listeners come
        # and go, so a fast key press after expect_file_chooser can open a real picker that
        # never closes and blocks later ones. A listener for the whole test keeps it on.
        self.page.on("filechooser", lambda chooser: None)

    def start(self, ad_id=1):
        self.sign_in()
        self.page.get_by_role("button", name=f"Preview ad #{ad_id}").click()
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)
        self.sent.clear()  # Signing in is not an edit.

    def figure(self, ad_format):
        return self.page.locator(f'#preview-frames figure[data-format="{ad_format}"]')

    def replace(self, ad_format="feed", name="Replace image"):
        return self.figure(ad_format).get_by_role("button", name=name, exact=True)

    def hold(self, path, method):
        """Hold each `method` request to `path` until the test continues it; return them."""
        held = []
        self.page.route(lambda url: urllib.parse.urlsplit(url).path == path,
                        lambda route: held.append(route) if route.request.method == method
                        else route.continue_())
        return held

    def pick(self, locator, name, content):
        with self.page.expect_file_chooser() as chooser:
            locator.click()
        chooser.value.set_files(self.media_file(name, content))

    def stored_ad(self, ad_id=1):
        status, body = call("GET", f"{self.url}/ads/{ad_id}")
        self.assertEqual(status, 200, body)
        return body

    def choosers(self):
        """Record every file chooser that opens from now on."""
        opened = []
        self.page.on("filechooser", lambda chooser: opened.append(chooser))
        return opened

    def assert_named_beside_button(self, name):
        """The media keeps its accessible name; the button over it covers exactly its frame."""
        dialog = self.page.locator("#preview-frames")
        expect(dialog.get_by_role("img", name=name, exact=True)).to_have_count(3)
        expect(dialog.get_by_role("button", name="Replace image", exact=True)).to_have_count(3)
        # A button's children are presentational to screen readers, so the media must not sit
        # inside it in Chromium's own tree (Playwright's role queries don't apply that rule).
        cdp = self.page.context.new_cdp_session(self.page)
        nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
        roles = {node["nodeId"]: node["role"]["value"] for node in nodes}
        parents = [roles.get(node.get("parentId")) for node in nodes if not node.get("ignored")
                   and (node["role"]["value"], node.get("name", {}).get("value")) == ("image", name)]
        self.assertEqual(len(parents), 3)
        self.assertNotIn("button", parents)
        for ad_format in FORMATS:
            figure = self.figure(ad_format)
            figure.scroll_into_view_if_needed()
            self.assertEqual(self.replace(ad_format).bounding_box(),
                             figure.get_by_role("img", name=name).bounding_box())

    def test_image_replace_cue_and_picker(self):
        self.start()
        self.assert_named_beside_button("Ad image")
        opacity = "(e) => getComputedStyle(e).opacity"
        for ad_format in FORMATS:
            with self.subTest(format=ad_format):
                button = self.replace(ad_format)
                cue = button.locator(".cue")
                self.page.mouse.move(0, 0)
                self.assertEqual(cue.evaluate(opacity), "0")
                self.assertEqual(self.figure(ad_format).locator("img").evaluate(
                    "(e) => getComputedStyle(e).filter"), "none")
                button.hover()
                expect(cue).to_have_text("Replace image")
                self.assertEqual(cue.evaluate(opacity), "1")
                self.assertNotEqual(cue.evaluate("(e) => getComputedStyle(e).backgroundColor"),
                                    "rgba(0, 0, 0, 0)")  # The frame dims.
                self.assertEqual(button.evaluate("(e) => getComputedStyle(e).cursor"), "pointer")
                self.page.mouse.move(0, 0)
                button.focus()
                self.assertEqual(cue.evaluate(opacity), "1")
                button.blur()
                with self.page.expect_file_chooser() as chooser:
                    button.click()
                self.assertEqual(chooser.value.element.get_attribute("accept"), self.IMAGES)
                self.assertFalse(chooser.value.is_multiple())
        self.assertEqual(self.sent, [])

    def test_unavailable_image_stays_named(self):
        image_hash = self.upload_via_api("images", b"not an image", filename="x.png")["hash"]
        self.publish_via_api("An ad whose image can't be shown", image_hash=image_hash)
        self.start(2)
        expect(self.page.locator("#preview-frames .media")).to_have_text(
            ["Media unavailable"] * 3)
        self.assert_named_beside_button("Media unavailable")
        with self.page.expect_file_chooser():
            self.replace("story").click()

    def test_replace_image_upload(self):
        self.start()
        held = self.hold("/accounts/42/images", "POST")
        self.pick(self.replace("story"), "new.png", real_png(b"new"))
        upload = self.wait_for(held)
        progress = self.figure("story").locator("progress")
        expect(progress).to_be_visible()
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(upload.request.url).query),
                         {"filename": ["new.png"]})
        upload.continue_()
        expect(self.page.locator("#preview-frames progress")).to_have_count(0)
        new_hash = self.wait_until_changed()
        images = self.page.locator("#preview-frames img")
        expect(images).to_have_count(3)
        for index in range(3):
            expect(images.nth(index)).to_have_attribute("src", re.compile(f"/{new_hash}\\?"))
        expect(self.replace("story")).to_be_focused()  # The re-render keeps focus.
        first = self.page.locator("#image-list li").first.get_by_role("radio")
        expect(first).to_have_attribute("value", f"image:{new_hash}")
        expect(first).not_to_be_checked()
        expect(self.page.locator("#image-list li")).to_have_count(2)
        self.assertEqual([method for method, *_ in self.sent], ["POST", "PATCH"])

    def wait_until_changed(self):
        """Wait for the stored ad's image to change; return the new hash."""
        for _ in range(50):
            image_hash = self.stored_ad()["image_hash"]
            if image_hash != self.old_hash:
                return image_hash
            self.page.wait_for_timeout(100)
        self.fail("The ad's image never changed")

    def test_replace_with_duplicate(self):
        existing = self.upload_via_api("images", real_png(b"dup"), filename="d.png",
                                       image_name="Existing")["hash"]
        self.start()
        statuses = []
        self.page.on("response", lambda response: response.request.method == "POST"
                     and statuses.append(response.status))
        self.pick(self.replace("feed"), "again.png", real_png(b"dup"))
        expect(self.page.locator("#preview-frames img").first).to_have_attribute(
            "src", re.compile(f"/{existing}\\?"))
        self.assertEqual(statuses, [409])
        self.assertEqual(self.stored_ad()["image_hash"], existing)
        expect(self.page.locator(f"#media-image-{existing}")).to_have_count(1)
        expect(self.page.locator("#preview-frames .error")).to_have_count(0)

    def test_wrong_type_file(self):
        self.start()
        error = self.figure("banner").locator(".error")
        for name, content in (("clip.mp4", test_api_e2e.MP4), ("notes.txt", b"hello")):
            with self.subTest(name=name):
                self.pick(self.replace("banner"), name, content)
                expect(error).to_have_text("Choose an image file.")
                expect(error).to_have_attribute("role", "alert")
                expect(self.replace("banner")).to_have_attribute(
                    "aria-describedby", "banner-edit-error")
                error.evaluate("(e) => { e.textContent = ''; }")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stored_ad()["image_hash"], self.old_hash)
        expect(self.replace("feed")).to_have_js_property("inert", False)

    def test_failed_update_keeps_upload(self):
        self.start()
        self.page.route("**/accounts/42/ads/1", lambda route: route.fulfill(
            status=400, content_type="application/json", body=jsonlib.dumps({
                "error_code": "002",
                "error_message": "The value provided for image_hash is invalid"}))
            if route.request.method == "PATCH" else route.continue_())
        self.pick(self.replace("feed"), "kept.png", real_png(b"kept"))
        expect(self.figure("feed").locator(".error")).to_have_text("Choose an image you uploaded.")
        self.assertEqual(self.stored_ad()["image_hash"], self.old_hash)
        status, listing = call("GET", f"{self.url}/accounts/42/images", token=self.token)
        self.assertEqual(status, 200, listing)
        [new_hash] = {image["hash"] for image in listing["data"]} - {self.old_hash}
        radio = self.page.locator("#image-list li").first.get_by_role("radio")
        expect(radio).to_have_attribute("value", f"image:{new_hash}")
        expect(self.page.locator("#preview-frames img").first).to_have_attribute(
            "src", re.compile(f"/{self.old_hash}\\?"))
        expect(self.page.locator("#preview-frames progress")).to_have_count(0)
        expect(self.figure("story").locator(".text-body")).to_have_js_property("inert", False)

    def test_late_upload_does_not_touch_new_dialog(self):
        self.publish_via_api("Second ad text", image_hash=self.old_hash)
        self.start()
        uploads = self.hold("/accounts/42/images", "POST")
        self.pick(self.replace("feed"), "late.png", real_png(b"late"))
        upload = self.wait_for(uploads)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#preview-dialog")).to_be_hidden()
        self.page.get_by_role("button", name="Preview ad #2").click()
        texts = self.page.locator("#preview-frames .text")
        expect(texts).to_have_text(["Second ad text"] * 3)
        # A re-render would drop this mark.
        self.page.locator("#preview-frames figure").evaluate_all(
            "(figures) => figures.forEach((figure) => { figure.dataset.mark = 'kept'; })")
        upload.continue_()
        new_hash = self.wait_until_changed()
        expect(self.page.locator("#ad-1")).to_contain_text(f"Image {new_hash[:8]}…")
        expect(self.page.locator("#preview-heading")).to_have_text("Preview of ad #2")
        expect(texts).to_have_text(["Second ad text"] * 3)
        expect(self.page.locator('#preview-frames figure[data-mark="kept"]')).to_have_count(3)
        images = self.page.locator("#preview-frames img")
        for index in range(3):
            expect(images.nth(index)).to_have_attribute("src", re.compile(f"/{self.old_hash}\\?"))
        expect(self.page.locator("#preview-dialog .error")).to_have_count(0)
        self.assertEqual(self.stored_ad(2)["image_hash"], self.old_hash)

    def test_reopening_waits_for_pending_upload(self):
        self.start()
        uploads = self.hold("/accounts/42/images", "POST")
        self.pick(self.replace("feed"), "late.png", real_png(b"late"))
        upload = self.wait_for(uploads)
        self.page.keyboard.press("Escape")
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-message")).to_have_text("Loading preview…")
        upload.continue_()
        new_hash = self.wait_until_changed()
        images = self.page.locator("#preview-frames img")
        expect(images).to_have_count(3)
        for index in range(3):
            expect(images.nth(index)).to_have_attribute("src", re.compile(f"/{new_hash}\\?"))

    def test_late_upload_failure_goes_to_status(self):
        self.publish_via_api("Second ad text", image_hash=self.old_hash)
        self.start()
        self.page.route("**/accounts/42/ads/1", lambda route: route.fulfill(
            status=400, content_type="application/json", body=jsonlib.dumps({
                "error_code": "002",
                "error_message": "The value provided for image_hash is invalid"}))
            if route.request.method == "PATCH" else route.continue_())
        uploads = self.hold("/accounts/42/images", "POST")
        self.pick(self.replace("feed"), "late.png", real_png(b"late"))
        upload = self.wait_for(uploads)
        self.page.keyboard.press("Escape")
        self.page.get_by_role("button", name="Preview ad #2").click()
        expect(self.page.locator("#preview-frames .text")).to_have_text(["Second ad text"] * 3)
        upload.continue_()
        expect(self.page.locator("#status")).to_have_text(
            "Ad #1: media not saved: The value provided for image_hash is invalid")
        expect(self.page.locator("#preview-dialog .error")).to_have_count(0)
        expect(self.page.locator("#preview-message")).to_have_text("")
        self.assertEqual(self.stored_ad()["image_hash"], self.old_hash)

    def test_video_playback_and_replace_control(self):
        video_id = self.upload_via_api("videos", self.webm(), filename="clip.webm",
                                       name="Clip")["id"]
        self.publish_via_api("Watch this clip", video_id=video_id)
        self.page.set_viewport_size({"width": 1280, "height": 1000})  # Story fits on screen.
        self.start(2)
        opened = self.choosers()
        for ad_format in FORMATS:
            with self.subTest(format=ad_format):
                figure = self.figure(ad_format)
                video = figure.locator("video")
                button = self.replace(ad_format, "Replace video")
                expect(video).to_have_js_property("readyState", 4)
                figure.scroll_into_view_if_needed()
                button.hover()
                expect(button).to_have_css("opacity", "1")
                frame, box = video.bounding_box(), button.bounding_box()
                frame_right, frame_bottom = frame["x"] + frame["width"], frame["y"] + frame["height"]
                self.assertLessEqual(box["x"] + box["width"], frame_right)
                self.assertGreater(box["x"] + box["width"], frame_right - 24)
                self.assertGreaterEqual(box["y"], frame["y"])
                self.assertLess(box["y"], frame["y"] + 24)
                self.assertLess(box["y"] + box["height"], frame_bottom - 40)
                if ad_format == "story":
                    header = figure.locator("header").bounding_box()
                    self.assertLess(box["y"], header["y"] + header["height"])  # Overlaps it,
                    self.assertTrue(button.evaluate("""(e) => { const b = e.getBoundingClientRect();
                        return document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
                            === e; }"""))  # and is drawn above it.
                paused = video.evaluate("(e) => e.paused")
                self.page.mouse.click(frame["x"] + frame["width"] / 2, frame["y"] + frame["height"] / 2)
                expect(video).to_have_js_property("paused", not paused)
        self.page.wait_for_timeout(300)
        self.assertEqual(opened, [])
        expect(self.page.locator("#preview-frames textarea")).to_have_count(0)
        self.assertEqual(self.sent, [])
        with self.page.expect_file_chooser() as chooser:
            self.replace("banner", "Replace video").click()
        self.assertEqual(chooser.value.element.get_attribute("accept"),
                         ".mp4,.mov,.m4v,.webm,.mkv,.avi")
        videos = self.page.locator("#preview-frames video")
        old_path = videos.first.get_attribute("data-path")
        chooser.value.set_files(self.media_file("My clip.final.webm", self.webm() + b"\0"))
        for index in range(3):  # Videos are served by content hash, not ID.
            expect(videos.nth(index)).not_to_have_attribute("data-path", old_path)
        self.assertEqual(self.stored_ad(2)["video_id"], video_id + 1)
        [(_, path, query)] = [sent for sent in self.sent if sent[0] == "POST"]
        self.assertEqual(path, "/accounts/42/videos")
        self.assertEqual(urllib.parse.parse_qs(query)["name"], ["My clip.final"])
        expect(self.page.locator("#video-list li").first).to_contain_text("My clip.final · ")

    def test_text_save_keeps_media_elements(self):
        video_id = self.upload_via_api("videos", self.webm(), filename="clip.webm",
                                       name="Clip")["id"]
        self.publish_via_api("Watch this clip", video_id=video_id)
        self.page.set_viewport_size({"width": 1280, "height": 1000})  # Story fits on screen.
        mark = "(elements) => elements.forEach((element) => { element.kept = true; })"
        kept = "(elements) => elements.map((element) => element.kept === true)"
        for ad_id, tag in ((1, "img"), (2, "video")):
            with self.subTest(media=tag):
                if ad_id == 1:
                    self.start()
                else:
                    self.page.keyboard.press("Escape")
                    self.page.get_by_role("button", name="Preview ad #2").click()
                media = self.page.locator(f"#preview-frames {tag}")
                expect(media).to_have_count(3)
                story = self.figure("story").locator(tag)
                if tag == "video":
                    expect(story).to_have_js_property("paused", False)
                media.evaluate_all(mark)
                text = f"Fresh words for ad {ad_id}"
                self.figure("story").locator(".text-body").click()
                self.page.locator("#preview-frames textarea").fill(text)
                self.page.locator("#preview-heading").click()
                expect(self.page.locator("#preview-frames .text-body")).to_have_text([text] * 3)
                self.assertEqual(media.evaluate_all(kept), [True] * 3)
                if tag == "video":
                    expect(story).to_have_js_property("paused", False)

    def test_tab_from_text_to_video_keeps_focus(self):
        video_id = self.upload_via_api("videos", self.webm(), filename="clip.webm",
                                       name="Clip")["id"]
        self.publish_via_api("Watch this clip", video_id=video_id)
        self.start(2)
        self.figure("feed").locator(".text-body").click()
        self.page.locator("#preview-frames textarea").fill("Brand new words for this ad")
        self.page.keyboard.press("Tab")
        video = self.figure("feed").locator("video")
        expect(video).to_be_focused()
        expect(self.page.locator("#preview-frames .text-body")).to_have_text(
            ["Brand new words for this ad"] * 3)
        expect(video).to_be_focused()

    def test_keyboard_editing(self):
        video_id = self.upload_via_api("videos", self.webm(), filename="clip.webm",
                                       name="Clip")["id"]
        self.publish_via_api("Watch this clip", video_id=video_id)
        self.start()
        for ad_id, name in ((1, "Replace image"), (2, "Replace video")):
            with self.subTest(name=name):
                if ad_id == 2:
                    self.page.keyboard.press("Escape")
                    self.page.get_by_role("button", name="Preview ad #2").click()
                    expect(self.page.locator("#preview-frames video")).to_have_count(3)
                reached = set()
                for _ in range(40):
                    self.page.keyboard.press("Tab")
                    reached.add(self.page.evaluate(f"""() => document.activeElement.matches(
                        '.replace[aria-label="{name}"]')
                        ? document.activeElement.closest('figure').dataset.format : null"""))
                self.assertLessEqual(set(FORMATS), reached)
                for ad_format in FORMATS:
                    button = self.replace(ad_format, name)
                    button.focus()
                    if name == "Replace video":
                        expect(button).to_have_css("opacity", "1")
                    with self.page.expect_file_chooser():
                        self.page.keyboard.press("Enter")
        self.assertEqual(self.sent, [])

    def test_one_edit_at_a_time(self):
        self.start()
        # During an upload, the text targets and other replace controls are inert.
        uploads = self.hold("/accounts/42/images", "POST")
        self.pick(self.replace("feed"), "new.png", real_png(b"new"))
        upload = self.wait_for(uploads)
        for ad_format in FORMATS:
            expect(self.figure(ad_format).locator(".text-body")).to_have_js_property("inert", True)
        expect(self.replace("story")).to_have_js_property("inert", True)
        opened = self.choosers()
        self.click_through(self.figure("story").locator(".text-body"))
        self.click_through(self.replace("story"))
        self.replace("feed").click(position={"x": 10, "y": 10})  # The running one ignores clicks.
        expect(self.page.locator("#preview-frames textarea")).to_have_count(0)
        upload.continue_()
        self.wait_until_changed()
        expect(self.figure("story").locator(".text-body")).to_have_js_property("inert", False)
        self.assertEqual(opened, [])
        self.assertEqual([method for method, *_ in self.sent], ["POST", "PATCH"])
        # During a text save, replace image opens nothing until the save lands.
        self.page.unroute_all()
        patches = self.hold("/accounts/42/ads/1", "PATCH")
        self.figure("feed").locator(".text-body").click()
        self.page.locator("#preview-frames textarea").fill("Brand new words for this ad")
        self.page.locator("#preview-heading").click()
        save = self.wait_for(patches)
        self.click_through(self.replace("story"))
        self.page.wait_for_timeout(300)
        self.assertEqual(opened, [])
        save.continue_()
        expect(self.page.locator("#preview-frames .text-body")).to_have_text(
            ["Brand new words for this ad"] * 3)
        with self.page.expect_file_chooser():
            self.replace("story").click()


class NoMediaKeyPreviewTests(UiCase):
    def test_no_media_key_placements_editable(self):
        self.create_login()
        image_hash = self.upload_via_api("images", png(), filename="a.png")["hash"]
        self.publish_via_api("An ad without a media key", image_hash=image_hash)
        self.sign_in()
        self.page.get_by_role("button", name="Preview ad #1").click()
        expect(self.page.locator("#preview-frames article.ad")).to_have_count(3)
        expect(self.page.locator("#preview-frames .text")).to_have_text(
            ["An ad without a media key"] * 3)
        boxes = self.page.locator("#preview-frames .media")
        expect(boxes).to_have_text(["Media unavailable"] * 3)
        for index in range(3):
            expect(boxes.nth(index)).to_have_attribute("aria-label", "Media unavailable")
        expect(self.page.locator("#preview-message")).to_have_text("")
        expect(self.page.locator("#preview-share")).to_be_hidden()
        self.page.locator('#preview-frames figure[data-format="feed"] .text-body').click()
        self.page.locator("#preview-frames textarea").fill("Edited without a media key")
        self.page.locator("#preview-heading").click()
        expect(self.page.locator("#preview-frames .text")).to_have_text(
            ["Edited without a media key"] * 3)
        self.assertEqual(call("GET", f"{self.url}/ads/1")[1]["text"], "Edited without a media key")


if __name__ == "__main__":
    unittest.main()
