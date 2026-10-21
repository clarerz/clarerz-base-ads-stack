"""Domain objects, independent of storage and publishing."""

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class AdAccount:
    id: int
    name: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AdDraft:
    """Validated input before image ingestion and assignment of ad metadata."""

    creator_id: int
    text: str
    image: Optional[str] = None
    image_hash: Optional[str] = None


@dataclass(frozen=True)
class Ad:
    id: int
    system_name: str
    image_hash: str
    text: str
    creator_id: int
    creation_time: int

    def to_dict(self):
        return asdict(self)
