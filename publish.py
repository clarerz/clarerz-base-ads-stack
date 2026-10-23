"""Dictionary API retained for existing callers; see AdsService for objects."""

import os
from pathlib import Path

from repository import SQLiteAdsRepository
from service import AdsService
from validation import ProjectionValidator, ValidationError

_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ.get("ADS_STACK_DATA_DIR", str(_BASE_DIR / "data"))).expanduser().resolve()
DB_PATH = str(_DATA_DIR / "ads.db")


def _service():
    return AdsService(SQLiteAdsRepository(DB_PATH))


def create_account(account_id, name):
    try:
        account = _service().create_account(account_id, name)
        return {"id": account.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return ValidationError("002", "account").to_dict()


def get_account(account_id):
    try:
        account = _service().get_account(account_id)
        return account.to_dict() if account is not None else None
    except Exception:
        return None


def publish_ad(data):
    try:
        ad = _service().publish_ad(data)
        return {"id": ad.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return ValidationError("002", "data").to_dict()


def get_ads(ad_id, fields=None):
    try:
        ad = _service().get_ad(ad_id)
    except Exception:
        return None
    if ad is None:
        return None
    try:
        return ProjectionValidator().project(ad, fields)
    except ValidationError as exc:
        return exc.to_dict()


def get_ad(ad_id):
    return get_ads(ad_id)


def upload_video(data):
    try:
        video = _service().upload_video(data)
        return {"id": video.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return ValidationError("002", "data").to_dict()


def rename_video(video_id, name):
    try:
        video = _service().rename_video(video_id, name)
        return {"id": video.id}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return ValidationError("002", "video").to_dict()


def list_videos(owner_id, limit=None, cursor=None, since=None, until=None):
    try:
        videos, next_cursor = _service().list_videos(owner_id, limit, cursor, since, until)
        return {"data": [video.to_dict() for video in videos], "next_cursor": next_cursor}
    except ValidationError as exc:
        return exc.to_dict()
    except Exception:
        return ValidationError("002", "videos").to_dict()
