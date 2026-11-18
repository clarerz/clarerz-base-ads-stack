"""Object API for accounts, ad publishing/reads, and image and video uploads."""

from dataclasses import replace
import re
import secrets
import time

import image_store
from models import User
from publishing import AdPublisher, store_owned_image
from validation import (AccountValidator, StorageError, UploadValidator, ValidationError,
                        VideoValidator, authenticate, coerce_id, decode_ad_cursor,
                        dummy_password_hash, encode_ad_cursor, encode_image_cursor,
                        encode_video_cursor, existing_account_id, hash_password, hash_token,
                        validate_name, validate_page, validate_password, validate_username,
                        verify_password)
import video_store


# Uploads record their size from this time on (2026-10-30T01:00:00Z, just after that shipped);
# earlier images without a stored size are listed with a placeholder. File entries written by
# older code have no size keys at all and get the placeholder whenever they were uploaded.
DIMENSIONS_CUTOFF = 1793322000
PLACEHOLDER_DIMENSIONS = (1080, 1080)
SESSION_SECONDS = 12 * 3600
# After this many consecutive failures a login is refused until THROTTLE_SECONDS after the
# latest one. ponytail: per user, so anyone who knows a (public) account ID can keep its
# login failing with one bad attempt per window; add per-IP limits before non-demo use.
MAX_FAILED_LOGINS = 5
THROTTLE_SECONDS = 30
_ACCOUNT_ID_LOGIN = re.compile(r"[0-9]+")  # ASCII digits only; str.isdigit accepts others.


class AdsService:
    def __init__(self, repository, publisher=None, account_validator=None, video_storage=None,
                 image_storage=None, clock=None, upload_validator=None, video_validator=None):
        self.repository = repository
        self.image_storage = image_storage if image_storage is not None else image_store
        self.clock = clock if clock is not None else time.time
        # The default publisher checks hashes against the storage uploads go into.
        self.publisher = (publisher if publisher is not None
                          else AdPublisher(repository, image_storage=self.image_storage,
                                           clock=self.clock))
        self.account_validator = (account_validator if account_validator is not None
                                  else AccountValidator())
        self.image_validator = (upload_validator if upload_validator is not None
                                else UploadValidator(repository))
        self.video_validator = (video_validator if video_validator is not None
                                else VideoValidator(repository))
        self.video_storage = video_storage if video_storage is not None else video_store

    def create_account(self, account_id, name):
        """Return (account, access_token). Only a hash is stored, so the token cannot be recovered."""
        account = self.account_validator.validate(account_id, name)
        token = secrets.token_urlsafe(32)
        if not self.repository.insert_account(account, hash_token(token)):
            raise ValidationError("002", "account_id")
        return account, token

    def rename_account(self, account_id, name, access_token):
        account = self.account_validator.validate(account_id, name)
        authenticate(self.repository, account.id, access_token)
        self.repository.rename_account(account.id, account.name)
        return account

    def issue_access_token(self, account_id):
        """Operator action: replace an account's token, or give a pre-authentication account one."""
        account_id = coerce_id(account_id)
        token = secrets.token_urlsafe(32)
        if account_id is None or not self.repository.set_token_hash(account_id, hash_token(token)):
            raise ValidationError("002", "account_id")
        return token

    def create_user(self, account_id, access_token, username, password):
        """Give the account its web login, proved by its current access token; return the User.

        The token is checked here and never stored or usable as the password. A login claimed
        with a token that has since been reissued is replaced.
        """
        account_id = existing_account_id(self.repository, account_id, "account_id")
        # Authenticate first, so only the account's owner learns whether a username is taken.
        authenticate(self.repository, account_id, access_token, sessions=False)
        user = User(account_id, validate_username(username),
                    hash_password(validate_password(password, access_token)),
                    hash_token(access_token))
        blocked = self.repository.create_user(user)
        if blocked is not None:
            raise ValidationError("002", blocked)
        return user

    def login(self, login, password, replaces=None):
        """Start a session for a username or account ID; return (token, user, account).

        Every failure raises the same 002 for login. replaces is the caller's previous
        session token, if any; it is ended.
        """
        now = int(self.clock())
        user = self._login_user(login)
        current = (user is not None
                   and self.repository.get_token_hash(user.account_id) == user.token_hash)
        # Exactly one scrypt run on every path, before anything else is decided, so response
        # time does not reveal whether the login exists, is stale, or is throttled.
        matches = verify_password(password if isinstance(password, str) else "",
                                  user.password_hash if current else dummy_password_hash())
        throttled = (current and user.failed_logins >= MAX_FAILED_LOGINS
                     and now - user.last_failure < THROTTLE_SECONDS)
        if current:
            self.repository.record_login(user.account_id, matches and not throttled, now)
        if not (current and matches and not throttled):
            raise ValidationError("002", "login")
        token = secrets.token_urlsafe(32)
        self.repository.create_session(
            hash_token(token), user.account_id, now + SESSION_SECONDS, now,
            replaces=hash_token(replaces) if isinstance(replaces, str) else None)
        return token, user, self.repository.get_account(user.account_id)

    def _login_user(self, login):
        if not isinstance(login, str):
            return None
        login = login.strip()
        if _ACCOUNT_ID_LOGIN.fullmatch(login):
            account_id = coerce_id(login)  # None past SQLite's range.
            return None if account_id is None else self.repository.get_user(account_id=account_id)
        return self.repository.get_user(username=login)

    def get_session(self, session_token):
        """Return (user, account) for a live session of a current login, else None."""
        now = int(self.clock())
        self.repository.delete_sessions(now=now)
        session = (self.repository.get_session(hash_token(session_token))
                   if isinstance(session_token, str) else None)
        if session is None or session[1] <= now:
            return None
        return (self.repository.get_user(account_id=session[0]),
                self.repository.get_account(session[0]))

    def logout(self, session_token):
        if isinstance(session_token, str):
            self.repository.delete_sessions(token_hash=hash_token(session_token))

    def get_account(self, account_id):
        account_id = coerce_id(account_id)
        return None if account_id is None else self.repository.get_account(account_id)

    def publish_ad(self, data):
        return self.publisher.publish(data)

    def update_ad(self, ad_id, data):
        return self.publisher.update(ad_id, data)

    def get_ad(self, ad_id):
        ad_id = coerce_id(ad_id)
        return None if ad_id is None else self.repository.get_ad(ad_id)

    def list_ads(self, creator_id, limit=None, cursor=None, since=None, until=None):
        """Return (ads, next_cursor) for one page of the creator's ads, newest first.

        Ads are public (see get_ad), so listing them needs no token.
        """
        creator_id = existing_account_id(self.repository, creator_id, "creator_id")
        limit, after, since, until = validate_page(limit, cursor, since, until, decode_ad_cursor)
        # Fetch one extra row to learn whether another page exists.
        ads = self.repository.list_ads(creator_id, limit + 1, after, since, until)
        page = ads[:limit]
        return page, encode_ad_cursor(page[-1]) if len(ads) > limit else None

    def upload_image(self, data):
        """Store an image for its authenticated owner and return the image's content hash."""
        owner_id, path, image_name = self.image_validator.validate(data)
        return store_owned_image(self.repository, self.image_storage, owner_id, path, "file",
                                 int(self.clock()), image_name)

    def list_images(self, owner_id, access_token, limit=None, cursor=None, since=None,
                    until=None):
        """Return (images, next_cursor) for one page of the owner's images, newest first."""
        owner_id, limit, after, since, until = self.image_validator.validate_query(
            owner_id, limit, cursor, since, until)
        authenticate(self.repository, owner_id, access_token)
        # Fetch one extra row to learn whether another page exists.
        images = self.repository.list_images(owner_id, limit + 1, after, since, until)
        page = [self.describe_image(image) for image in images[:limit]]
        return page, encode_image_cursor(page[-1]) if len(images) > limit else None

    def get_image(self, owner_id, access_token, image_hash=None, image_name=None):
        """Return the owner's image, found by hash or by name, with its URL and dimensions."""
        owner_id, image_hash, image_name = self.image_validator.validate_lookup(
            owner_id, image_hash, image_name)
        authenticate(self.repository, owner_id, access_token)
        image = self.repository.get_image(owner_id, image_hash, image_name)
        if image is None:
            # Unknown images and other accounts' images get the same error.
            raise ValidationError("002", "image_hash" if image_hash is not None else "image_name")
        return self.describe_image(image)

    def describe_image(self, image):
        """Add the stored URL and dimensions to an owned image."""
        # ponytail: one registry lookup (a full JSON parse) per listed image; read the
        # registry once per page if listings get slow.
        try:
            entry = self.image_storage.get_image_by_hash(image.hash)
        except Exception as exc:
            raise StorageError("image") from exc
        entry = entry if isinstance(entry, dict) else {}
        size = (entry.get("width"), entry.get("height"))
        if not all(type(side) is int and side > 0 for side in size):
            # Images from before sizes were recorded (by time, or file entries with no size
            # keys) get a placeholder; newer ones with no known size report None.
            legacy_file = entry.get("source") == "file_handle" and "width" not in entry
            size = (PLACEHOLDER_DIMENSIONS
                    if legacy_file or image.creation_time < DIMENSIONS_CUTOFF else (None, None))
        return replace(image, image_url=entry.get("url"), width=size[0], height=size[1])

    def upload_video(self, data):
        owner_id, name, path = self.video_validator.validate(data)
        try:
            video_hash = self.video_storage.store_video(path)
        except ValueError as exc:  # Oversized file.
            raise ValidationError("002", "file") from exc
        except Exception as exc:
            raise StorageError("file") from exc
        # ponytail: bytes are stored before the row, so a failed insert leaves an
        # unreferenced (deduplicated) file; add a cleanup sweep if that matters.
        return self.repository.save_video(owner_id, name, video_hash, int(self.clock()))

    def get_video(self, video_id, access_token):
        """Return the video to its owner."""
        return self._owned_video(video_id, access_token)

    def rename_video(self, video_id, name, access_token=None):
        # Check the ID before the name, so an invalid ID is reported first.
        if coerce_id(video_id) is None:
            raise ValidationError("002", "video_id")
        name = validate_name(name)
        video = self._owned_video(video_id, access_token)
        return self.repository.rename_video(video.id, name)

    def _owned_video(self, video_id, access_token):
        video_id = coerce_id(video_id)
        if video_id is None:
            raise ValidationError("002", "video_id")
        try:
            video = self.repository.get_video(video_id)
        except Exception as exc:
            raise StorageError("video") from exc
        if video is None:
            raise ValidationError("002", "video_id")
        # A wrong token gets the same error as a missing video, and a failed token lookup the
        # same as a failed video lookup, so callers cannot probe IDs.
        try:
            authenticate(self.repository, video.owner_id, access_token, field="video_id")
        except StorageError as exc:
            raise StorageError("video") from exc
        return video

    def list_videos(self, owner_id, access_token, limit=None, cursor=None, since=None,
                    until=None):
        """Return (videos, next_cursor) for one page; next_cursor is None on the last page."""
        owner_id, limit, after, since, until = self.video_validator.validate_query(
            owner_id, limit, cursor, since, until)
        authenticate(self.repository, owner_id, access_token)
        # Fetch one extra row to learn whether another page exists.
        videos = self.repository.list_videos(owner_id, limit + 1, after, since, until)
        page = videos[:limit]
        return page, encode_video_cursor(page[-1]) if len(videos) > limit else None
