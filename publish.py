"""Dictionary API retained for existing callers; see AdsService for objects."""

from functools import wraps

import config
from repository import SQLiteAdsRepository
from service import AdsService
from validation import ProjectionValidator, StorageError, ValidationError

DB_PATH = str(config.DATA_DIR / "ads.db")


def _service():
    return AdsService(SQLiteAdsRepository(DB_PATH))


def _as_dict(resource):
    """Return a ValidationError as its error dict, and any other failure as 003 for resource."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ValidationError as exc:
                return exc.to_dict()
            except Exception:
                return StorageError(resource).to_dict()
        return wrapper
    return decorate


@_as_dict("account")
def create_account(account_id, name, access_token=None):
    """Create an account and return its one-time access token; with a token, rename it instead."""
    service = _service()
    if access_token is None:
        account, token = service.create_account(account_id, name)
        return {"id": account.id, "access_token": token}
    return {"id": service.rename_account(account_id, name, access_token).id}


@_as_dict("account")
def get_account(account_id):
    account = _service().get_account(account_id)
    return account.to_dict() if account is not None else None


@_as_dict("ad")
def publish_ad(data):
    return {"id": _service().publish_ad(data).id}


@_as_dict("ad")
def get_ads(ad_id, fields=None):
    """Return the ad or requested fields, None if it is missing, or an error dict."""
    ad = _service().get_ad(ad_id)
    return ProjectionValidator().project(ad, fields) if ad is not None else None


def get_ad(ad_id):
    return get_ads(ad_id)


@_as_dict("ad")
def list_ads(creator_id, limit=None, cursor=None, since=None, until=None):
    ads, next_cursor = _service().list_ads(creator_id, limit, cursor, since, until)
    return {"data": [ad.to_dict() for ad in ads], "next_cursor": next_cursor}


@_as_dict("image")
def upload_image(data):
    return {"hash": _service().upload_image(data)}


@_as_dict("image")
def get_image(owner_id, access_token, image_hash=None, image_name=None):
    """Return one of the owner's images, by hash or by name, or an error dict."""
    return _service().get_image(owner_id, access_token, image_hash, image_name).to_dict()


@_as_dict("image")
def list_images(owner_id, access_token, limit=None, cursor=None, since=None, until=None):
    images, next_cursor = _service().list_images(owner_id, access_token, limit, cursor,
                                                 since, until)
    return {"data": [image.to_dict() for image in images], "next_cursor": next_cursor}


@_as_dict("video")
def upload_video(data):
    return {"id": _service().upload_video(data).id}


@_as_dict("video")
def get_video(video_id, access_token):
    return _service().get_video(video_id, access_token).to_dict()


@_as_dict("video")
def rename_video(video_id, name, access_token=None):
    return {"id": _service().rename_video(video_id, name, access_token).id}


@_as_dict("video")
def list_videos(owner_id, access_token, limit=None, cursor=None, since=None, until=None):
    videos, next_cursor = _service().list_videos(owner_id, access_token, limit, cursor,
                                                 since, until)
    return {"data": [video.to_dict() for video in videos], "next_cursor": next_cursor}
