"""Object API for accounts, ad publishing/reads, and image and video uploads."""

import secrets
import time

import image_store
from publishing import AdPublisher, store_owned_image
from validation import (AccountValidator, UploadValidator, ValidationError, VideoValidator,
                        authenticate, coerce_id, encode_image_cursor, encode_video_cursor,
                        hash_token, validate_name)
import video_store


class AdsService:
    def __init__(self, repository, publisher=None, account_validator=None, video_storage=None,
                 image_storage=None):
        self.repository = repository
        self.publisher = publisher if publisher is not None else AdPublisher(repository)
        self.account_validator = (account_validator if account_validator is not None
                                  else AccountValidator())
        self.image_validator = UploadValidator(repository)
        self.video_validator = VideoValidator(repository)
        self.video_storage = video_storage if video_storage is not None else video_store
        self.image_storage = image_storage if image_storage is not None else image_store

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

    def upload_image(self, data):
        """Store an image for its authenticated owner and return the image's content hash."""
        owner_id, path, image_name = self.image_validator.validate(data)
        return store_owned_image(self.repository, self.image_storage, owner_id, path, "file",
                                 int(time.time()), image_name)

    def list_images(self, owner_id, access_token, limit=None, cursor=None, since=None,
                    until=None):
        """Return (images, next_cursor) for one page of the owner's images, newest first."""
        owner_id, limit, after, since, until = self.image_validator.validate_query(
            owner_id, limit, cursor, since, until)
        authenticate(self.repository, owner_id, access_token)
        # Fetch one extra row to learn whether another page exists.
        images = self.repository.list_images(owner_id, limit + 1, after, since, until)
        page = images[:limit]
        return page, encode_image_cursor(page[-1]) if len(images) > limit else None

    def upload_video(self, data):
        owner_id, name, path = self.video_validator.validate(data)
        try:
            video_hash = self.video_storage.store_video(path)
        except Exception as exc:
            raise ValidationError("002", "file") from exc
        # ponytail: bytes are stored before the row, so a failed insert leaves an
        # unreferenced (deduplicated) file; add a cleanup sweep if that matters.
        return self.repository.save_video(owner_id, name, video_hash, int(time.time()))

    def rename_video(self, video_id, name, access_token=None):
        video_id = coerce_id(video_id)
        if video_id is None:
            raise ValidationError("002", "video_id")
        name = validate_name(name)
        video = self.repository.get_video(video_id)
        if video is None:
            raise ValidationError("002", "video_id")
        # A wrong token gets the same error as a missing video, so callers cannot probe IDs.
        authenticate(self.repository, video.owner_id, access_token, field="video_id")
        return self.repository.rename_video(video_id, name)

    def list_videos(self, owner_id, limit=None, cursor=None, since=None, until=None):
        """Return (videos, next_cursor) for one page; next_cursor is None on the last page."""
        owner_id, limit, after, since, until = self.video_validator.validate_query(
            owner_id, limit, cursor, since, until)
        # Fetch one extra row to learn whether another page exists.
        videos = self.repository.list_videos(owner_id, limit + 1, after, since, until)
        page = videos[:limit]
        return page, encode_video_cursor(page[-1]) if len(videos) > limit else None
