"""Dictionary API retained for existing callers; see AdsService for objects."""

import config
from repository import SQLiteAdsRepository
from service import AdsService
from validation import ProjectionValidator, StorageError, ValidationError

DB_PATH = str(config.DATA_DIR / "ads.db")


def _service():
    return AdsService(SQLiteAdsRepository(DB_PATH))


def create_account(account_id, name, access_token=None):
    """Create an account and return its one-time access token; with a token, rename it instead."""
    try:
        service = _service()
        if access_token is None:
            account, token = service.create_account(account_id, name)
            return {"id": account.id, "access_token": token}
        return {"id": service.rename_account(account_id, name, access_token).id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("account").to_dict()


def get_account(account_id):
    try:
        account = _service().get_account(account_id)
    except Exception:
        return StorageError("account").to_dict()
    return account.to_dict() if account is not None else None


def publish_ad(data):
    try:
        ad = _service().publish_ad(data)
        return {"id": ad.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("ad").to_dict()


def get_ads(ad_id, fields=None):
    """Return the ad or requested fields, None if it is missing, or an error dict."""
    try:
        ad = _service().get_ad(ad_id)
    except Exception:
        return StorageError("ad").to_dict()
    if ad is None:
        return None
    try:
        return ProjectionValidator().project(ad, fields)
    except ValidationError as exc:
        return exc.to_dict()


def get_ad(ad_id):
    return get_ads(ad_id)


def list_ads(creator_id, limit=None, cursor=None, since=None, until=None):
    try:
        ads, next_cursor = _service().list_ads(creator_id, limit, cursor, since, until)
        return {"data": [ad.to_dict() for ad in ads], "next_cursor": next_cursor}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("ad").to_dict()


def upload_image(data):
    try:
        return {"hash": _service().upload_image(data)}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("image").to_dict()


def list_images(owner_id, access_token, limit=None, cursor=None, since=None, until=None):
    try:
        images, next_cursor = _service().list_images(owner_id, access_token, limit, cursor,
                                                     since, until)
        return {"data": [image.to_dict() for image in images], "next_cursor": next_cursor}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("image").to_dict()


def upload_video(data):
    try:
        video = _service().upload_video(data)
        return {"id": video.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("video").to_dict()


def get_video(video_id, access_token):
    try:
        return _service().get_video(video_id, access_token).to_dict()
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("video").to_dict()


def rename_video(video_id, name, access_token=None):
    try:
        video = _service().rename_video(video_id, name, access_token)
        return {"id": video.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("video").to_dict()


def list_videos(owner_id, access_token, limit=None, cursor=None, since=None, until=None):
    try:
        videos, next_cursor = _service().list_videos(owner_id, access_token, limit, cursor,
                                                     since, until)
        return {"data": [video.to_dict() for video in videos], "next_cursor": next_cursor}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return StorageError("video").to_dict()
