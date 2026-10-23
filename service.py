"""Object API for account management, ad publishing/reads, and video uploads."""

import time

from publishing import AdPublisher
from validation import (AccountValidator, ValidationError, VideoValidator, coerce_id,
                        encode_cursor, validate_video_name)
import video_store


class AdsService:
    def __init__(self, repository, publisher=None, account_validator=None, video_storage=None):
        self.repository = repository
        self.publisher = publisher if publisher is not None else AdPublisher(repository)
        self.account_validator = (account_validator if account_validator is not None
                                  else AccountValidator())
        self.video_validator = VideoValidator(repository)
        self.video_storage = video_storage if video_storage is not None else video_store

    def create_account(self, account_id, name):
        account = self.account_validator.validate(account_id, name)
        self.repository.save_account(account)
        return account

    def get_account(self, account_id):
        account_id = coerce_id(account_id)
        return None if account_id is None else self.repository.get_account(account_id)

    def publish_ad(self, data):
        return self.publisher.publish(data)

    def get_ad(self, ad_id):
        ad_id = coerce_id(ad_id)
        return None if ad_id is None else self.repository.get_ad(ad_id)

    def upload_video(self, data):
        owner_id, name, path = self.video_validator.validate(data)
        try:
            video_hash = self.video_storage.store_video(path)
        except Exception as exc:
            raise ValidationError("002", "file") from exc
        # ponytail: bytes are stored before the row, so a failed insert leaves an
        # unreferenced (deduplicated) file; add a cleanup sweep if that matters.
        return self.repository.save_video(owner_id, name, video_hash, int(time.time()))

    def rename_video(self, video_id, name):
        video_id = coerce_id(video_id)
        if video_id is None:
            raise ValidationError("002", "video_id")
        video = self.repository.rename_video(video_id, validate_video_name(name))
        if video is None:
            raise ValidationError("002", "video_id")
        return video

    def list_videos(self, owner_id, limit=None, cursor=None, since=None, until=None):
        """Return (videos, next_cursor) for one page; next_cursor is None on the last page."""
        owner_id, limit, after, since, until = self.video_validator.validate_query(
            owner_id, limit, cursor, since, until)
        # Fetch one extra row to learn whether another page exists.
        videos = self.repository.list_videos(owner_id, limit + 1, after, since, until)
        page = videos[:limit]
        return page, encode_cursor(page[-1]) if len(videos) > limit else None
