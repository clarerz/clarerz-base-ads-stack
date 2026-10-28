"""Extensible publishing pipeline: validate, ingest image, persist ad."""

from dataclasses import replace
import time
from typing import Protocol

import image_store
from validation import AdValidator, HASH_REGEX, ValidationError


class ImageStorage(Protocol):
    def store_image(self, image_input: str) -> dict: ...


def store_owned_image(repository, image_storage, owner_id, image_input, field):
    """Ingest an image, record owner_id as an owner of its hash, and return the hash."""
    try:
        image_hash = image_storage.store_image(image_input)["hash"]
        if not isinstance(image_hash, str) or not HASH_REGEX.fullmatch(image_hash):
            raise ValueError("Image storage returned an invalid handle")
    except Exception as exc:
        raise ValidationError("002", field) from exc
    repository.grant_image(owner_id, image_hash)
    return image_hash


class AdPublisher:
    def __init__(self, repository, validator=None, image_storage=None, clock=None):
        self.repository = repository
        self.validator = validator if validator is not None else AdValidator(repository)
        self.image_storage = image_storage if image_storage is not None else image_store
        self.clock = clock if clock is not None else time.time

    def publish(self, data):
        draft = self.validator.validate(data)
        draft = self.normalize_image(draft)
        return self.persist(draft)

    def normalize_image(self, draft):
        if draft.image is None:
            return draft
        image_hash = store_owned_image(self.repository, self.image_storage, draft.creator_id,
                                       draft.image, "image")
        return replace(draft, image=None, image_hash=image_hash)

    def persist(self, draft):
        return self.repository.save_ad(draft, int(self.clock()))
