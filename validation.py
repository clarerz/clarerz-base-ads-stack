"""Validation rules without image ingestion or ad/account writes."""

import hashlib
import hmac
import os
from pathlib import Path
import re

import image_store
from models import AdAccount, AdDraft

URL_REGEX = re.compile(r"^https?://[^\s]+$")
HASH_REGEX = re.compile(r"^[A-Za-z0-9]{16,64}$")
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"})
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
_MAX_SQLITE_INT = 2**63 - 1


class ValidationError(ValueError):
    def __init__(self, code, field):
        self.code = code
        self.field = field
        message = (f"{field} is an invalid field on this endpoint" if code == "001"
                   else f"The value provided for {field} is invalid")
        super().__init__(message)

    def to_dict(self):
        return {"error_code": self.code, "error_message": str(self)}


def coerce_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def hash_token(token):
    # Tokens are 256-bit random values, so a fast unsalted hash is enough to store them.
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(repository, account_id, token, field="access_token"):
    """Raise ValidationError unless token is the account's current access token."""
    try:
        stored = repository.get_token_hash(account_id)
    except Exception as exc:
        raise ValidationError("002", field) from exc
    # Accounts created before authentication have no token until one is issued.
    if (not isinstance(stored, str) or not isinstance(token, str) or not token.isascii()
            or not hmac.compare_digest(stored, hash_token(token))):
        raise ValidationError("002", field)


class AccountValidator:
    def validate(self, account_id, name):
        account_id = coerce_id(account_id)
        if account_id is None:
            raise ValidationError("002", "account_id")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("002", "name")
        return AdAccount(account_id, name.strip())


class AdValidator:
    allowed_fields = frozenset({"creator_id", "access_token", "text", "image", "image_hash",
                                "video_id"})

    def __init__(self, repository, base_dir=None, rules=(), image_storage=None):
        self.repository = repository
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        # Must be the storage the publisher ingests into, so stored hashes resolve.
        self.image_storage = image_storage if image_storage is not None else image_store
        # Extra rules accept an AdDraft and raise ValidationError on rejection.
        self.rules = tuple(rules)

    def validate(self, data):
        if not isinstance(data, dict):
            raise ValidationError("002", "data")
        for field in data:
            if field not in self.allowed_fields:
                raise ValidationError("001", field)
        if "text" not in data:
            raise ValidationError("002", "text")
        has_image = "image" in data or "image_hash" in data
        if not has_image and "video_id" not in data:
            raise ValidationError("002", "image")
        if has_image and "video_id" in data:
            # An ad is either an image ad or a video ad.
            raise ValidationError("002", "video_id")
        if "creator_id" not in data:
            raise ValidationError("002", "creator_id")
        creator_id = self.validate_creator(data["creator_id"])
        # Authenticate before ownership checks so they reveal nothing to other callers.
        authenticate(self.repository, creator_id, data.get("access_token"))
        text = self.validate_text(data["text"])
        image = self.validate_image(data["image"]) if "image" in data else None
        image_hash = (self.validate_hash(data["image_hash"], creator_id)
                      if "image_hash" in data else None)
        video_id = (self.validate_video(data["video_id"], creator_id)
                    if "video_id" in data else None)
        draft = AdDraft(creator_id, text, image, image_hash, video_id)
        for rule in self.rules:
            rule(draft)
        return draft

    def validate_creator(self, value):
        creator_id = coerce_id(value)
        if creator_id is None:
            raise ValidationError("002", "creator_id")
        try:
            account = self.repository.get_account(creator_id)
        except Exception as exc:
            raise ValidationError("002", "creator_id") from exc
        if account is None:
            raise ValidationError("002", "creator_id")
        return creator_id

    def validate_text(self, value):
        if not isinstance(value, str) or len(value.strip()) < 3:
            raise ValidationError("002", "text")
        value = value.strip()
        if URL_REGEX.match(value) or HASH_REGEX.match(value):
            raise ValidationError("002", "text")
        return value

    def validate_image(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("002", "image")
        value = value.strip()
        if value.startswith(("http://", "https://")):
            if not URL_REGEX.match(value):
                raise ValidationError("002", "image")
            return value
        candidates = (
            Path(value), self.base_dir / value,
            self.base_dir / "file_handle_store" / value,
            self.base_dir / "file_handle_store" / os.path.basename(value),
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        raise ValidationError("002", "image")

    def validate_hash(self, value, creator_id):
        # The hash must name an image in storage that the creator uploaded or ingested.
        # Missing, unowned, and unresolvable hashes get the same error.
        if not isinstance(value, str) or not HASH_REGEX.match(value.strip()):
            raise ValidationError("002", "image_hash")
        value = value.strip()
        try:
            valid = (self.repository.owns_image(creator_id, value)
                     and self.image_storage.get_image_by_hash(value) is not None)
        except Exception as exc:
            raise ValidationError("002", "image_hash") from exc
        if not valid:
            raise ValidationError("002", "image_hash")
        return value

    def validate_video(self, value, creator_id):
        # The creator must own the video. Missing and unowned videos get the same
        # error so callers cannot probe which video IDs exist.
        video_id = coerce_id(value)
        if video_id is None:
            raise ValidationError("002", "video_id")
        try:
            video = self.repository.get_video(video_id)
        except Exception as exc:
            raise ValidationError("002", "video_id") from exc
        if video is None or video.owner_id != creator_id:
            raise ValidationError("002", "video_id")
        return video_id


def validate_name(value, field="name"):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("002", field)
    return value.strip()


class UploadValidator:
    """Image uploads and listings; videos extend it. Uploads authenticate the owner first."""

    required_fields = ("owner_id", "access_token", "file")
    optional_fields = ("image_name",)

    def __init__(self, repository):
        self.repository = repository

    def validate(self, data):
        """Return (owner_id, absolute file path, image name or None) for an image upload."""
        owner_id = self.validate_request(data)
        image_name = (validate_name(data["image_name"], "image_name")
                      if "image_name" in data else None)
        return owner_id, self.validate_file(data["file"]), image_name

    def validate_request(self, data):
        """Check field names, then return the authenticated owner ID."""
        if not isinstance(data, dict):
            raise ValidationError("002", "data")
        for field in data:
            if field not in self.required_fields + self.optional_fields:
                raise ValidationError("001", field)
        for field in self.required_fields:
            if field not in data:
                raise ValidationError("002", field)
        owner_id = self.validate_owner(data["owner_id"])
        authenticate(self.repository, owner_id, data["access_token"])
        return owner_id

    def validate_query(self, owner_id, limit=None, cursor=None, since=None, until=None):
        """Return (owner_id, limit, cursor position, since, until) for a listing."""
        owner_id = self.validate_owner(owner_id)
        if limit is None:
            limit = DEFAULT_PAGE_SIZE
        else:
            limit = coerce_id(limit)
            if limit is None or not 1 <= limit <= MAX_PAGE_SIZE:
                raise ValidationError("002", "limit")
        return (owner_id, limit, self.decode_cursor(cursor),
                _optional_time(since, "since"), _optional_time(until, "until"))

    def decode_cursor(self, value):
        return decode_image_cursor(value)

    def validate_owner(self, value):
        owner_id = coerce_id(value)
        if owner_id is None:
            raise ValidationError("002", "owner_id")
        try:
            account = self.repository.get_account(owner_id)
        except Exception as exc:
            raise ValidationError("002", "owner_id") from exc
        if account is None:
            raise ValidationError("002", "owner_id")
        return owner_id

    def validate_file(self, value):
        # Uploads come from local files only; storage enforces the size limit.
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("002", "file")
        path = Path(value.strip()).expanduser()
        if not path.is_file():
            raise ValidationError("002", "file")
        return str(path.resolve())


class VideoValidator(UploadValidator):
    required_fields = ("owner_id", "access_token", "name", "file")
    optional_fields = ()

    def validate(self, data):
        """Return (owner_id, name, absolute file path) for a valid upload request."""
        owner_id = self.validate_request(data)
        return owner_id, validate_name(data["name"]), self.validate_file(data["file"])

    def validate_file(self, value):
        # The extension is the only video format check.
        path = super().validate_file(value)
        if Path(value.strip()).suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValidationError("002", "file")
        return path

    def decode_cursor(self, value):
        return decode_video_cursor(value)



def _optional_time(value, field):
    if value is None:
        return None
    timestamp = coerce_id(value)
    if timestamp is None or not 0 <= timestamp <= _MAX_SQLITE_INT:
        raise ValidationError("002", field)
    return timestamp


def encode_video_cursor(video):
    return f"{video.upload_time}:{video.id}"


def decode_video_cursor(value):
    """Return the (upload_time, id) position after which the next page starts."""
    if value is None:
        return None
    parts = value.split(":") if isinstance(value, str) else ()
    position = tuple(coerce_id(part) for part in parts)
    if len(position) != 2 or None in position or max(position) > _MAX_SQLITE_INT:
        raise ValidationError("002", "cursor")
    return position


def encode_image_cursor(image):
    return f"{image.creation_time}:{image.hash}"


def decode_image_cursor(value):
    """Return the (creation_time, hash) position after which the next page starts."""
    if value is None:
        return None
    timestamp, _, image_hash = value.partition(":") if isinstance(value, str) else ("", "", "")
    timestamp = coerce_id(timestamp)
    if (timestamp is None or timestamp > _MAX_SQLITE_INT
            or not HASH_REGEX.fullmatch(image_hash)):
        raise ValidationError("002", "cursor")
    return timestamp, image_hash


class ProjectionValidator:
    def project(self, ad, fields=None):
        data = ad.to_dict()
        if fields is None:
            return data
        if isinstance(fields, str):
            fields = [fields]
        if not isinstance(fields, (list, tuple, set)):
            raise ValidationError("002", "fields")
        if not fields:
            return data
        for field in fields:
            if not isinstance(field, str) or field not in data:
                raise ValidationError("001", field)
        return {field: data[field] for field in fields}

