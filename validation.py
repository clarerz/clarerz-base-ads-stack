"""Validation rules without image ingestion or ad/account writes."""

import os
from pathlib import Path
import re

from models import AdAccount, AdDraft

URL_REGEX = re.compile(r"^https?://[^\s]+$")
HASH_REGEX = re.compile(r"^[A-Za-z0-9]{16,64}$")


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


class AccountValidator:
    def validate(self, account_id, name):
        account_id = coerce_id(account_id)
        if account_id is None:
            raise ValidationError("002", "account_id")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("002", "name")
        return AdAccount(account_id, name.strip())


class AdValidator:
    allowed_fields = frozenset({"creator_id", "text", "image", "image_hash"})

    def __init__(self, repository, base_dir=None, rules=()):
        self.repository = repository
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
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
        if "image" not in data and "image_hash" not in data:
            raise ValidationError("002", "image")
        if "creator_id" not in data:
            raise ValidationError("002", "creator_id")
        creator_id = self.validate_creator(data["creator_id"])
        text = self.validate_text(data["text"])
        image = self.validate_image(data["image"]) if "image" in data else None
        image_hash = self.validate_hash(data["image_hash"]) if "image_hash" in data else None
        draft = AdDraft(creator_id, text, image, image_hash)
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

    def validate_hash(self, value):
        if not isinstance(value, str) or not HASH_REGEX.match(value.strip()):
            raise ValidationError("002", "image_hash")
        return value.strip()


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
