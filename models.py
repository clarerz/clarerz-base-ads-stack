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
    video_id: Optional[int] = None


@dataclass(frozen=True)
class Ad:
    """An image ad has image_hash; a video ad has video_id. Never both."""

    id: int
    system_name: str
    image_hash: Optional[str]
    text: str
    creator_id: int
    creation_time: int
    video_id: Optional[int] = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Video:
    """Only the name can change, through a persisted rename; other fields are fixed at upload."""

    id: int
    name: str
    owner_id: int
    upload_time: int
    video_hash: str

    def to_dict(self):
        return asdict(self)
