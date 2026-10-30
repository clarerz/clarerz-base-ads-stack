"""Object API for accounts, ad publishing/reads, and image and video uploads."""

from dataclasses import replace
import secrets
import time

import image_store
from publishing import AdPublisher, store_owned_image
from validation import (AccountValidator, StorageError, UploadValidator, ValidationError,
                        VideoValidator, authenticate, coerce_id, decode_ad_cursor,
                        encode_ad_cursor, encode_image_cursor, encode_video_cursor,
                        existing_account_id, hash_token, validate_name, validate_page)
import video_store


# Uploads record their size from this time on (2026-10-30T01:00:00Z, just after that shipped);
# earlier images without a stored size are listed with a placeholder. File entries written by
# older code have no size keys at all and get the placeholder whenever they were uploaded.
DIMENSIONS_CUTOFF = 1793322000
PLACEHOLDER_DIMENSIONS = (1080, 1080)


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

    def get_account(self, account_id):
        account_id = coerce_id(account_id)
        return None if account_id is None else self.repository.get_account(account_id)

    def publish_ad(self, data):
        return self.publisher.publish(data)

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
        page = [self._describe(image) for image in images[:limit]]
        return page, encode_image_cursor(page[-1]) if len(images) > limit else None

    def _describe(self, image):
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
