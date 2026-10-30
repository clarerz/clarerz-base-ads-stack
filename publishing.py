"""Extensible publishing pipeline: validate, ingest image, persist ad."""

from dataclasses import replace
import time
from typing import Optional, Protocol

import image_store
from validation import AdValidator, HASH_REGEX, StorageError, ValidationError


class ImageStorage(Protocol):
    def store_image(self, image_input: str) -> dict: ...
    def get_image_by_hash(self, hash_val: str) -> Optional[dict]: ...


def store_owned_image(repository, image_storage, owner_id, image_input, field, creation_time,
                      image_name=None):
    """Ingest an image, record owner_id as an owner of its hash, and return the hash."""
    # Image storage raises ValueError for unusable input (missing or oversized files);
    # anything else, including a malformed handle, is a storage failure.
    try:
        image_hash = image_storage.store_image(image_input)["hash"]
    except ValueError as exc:
        raise ValidationError("002", field) from exc
    except Exception as exc:
        raise StorageError(field) from exc
    if not isinstance(image_hash, str) or not HASH_REGEX.fullmatch(image_hash):
        raise StorageError(field)
    # ponytail: a name conflict is only detected after the bytes are stored, so they stay
    # stored but unowned; check the name first if orphaned files matter.
    if not repository.grant_image(owner_id, image_hash, creation_time, image_name):
        raise ValidationError("002", "image_name")
    return image_hash


class AdPublisher:
    def __init__(self, repository, validator=None, image_storage=None, clock=None):
        self.repository = repository
        self.image_storage = image_storage if image_storage is not None else image_store
        self.validator = (validator if validator is not None
                          else AdValidator(repository, image_storage=self.image_storage))
        self.clock = clock if clock is not None else time.time

    def publish(self, data):
        draft = self.validator.validate(data)
        # Read the clock once, so the image ownership and the ad share a timestamp; the
        # publisher owns this time, so any value a validator set is replaced.
        draft = replace(draft, creation_time=int(self.clock()))
        draft = self.normalize_image(draft)
        return self.persist(draft)

    def _time_for(self, draft):
        # Stages called directly, without publish(), read the clock themselves.
        return draft.creation_time if draft.creation_time is not None else int(self.clock())

    def normalize_image(self, draft):
        if draft.image is None:
            return draft
        image_hash = store_owned_image(self.repository, self.image_storage, draft.creator_id,
                                       draft.image, "image", self._time_for(draft))
        return replace(draft, image=None, image_hash=image_hash)

    def persist(self, draft):
        return self.repository.save_ad(draft, self._time_for(draft))
