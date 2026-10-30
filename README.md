# Simple Ads Stack

A small Python base for authenticated ad accounts, image and video uploads, ad
publishing, and persistent reads. Uses only the Python standard library; no installation or server is needed.

## Quick start

```bash
# Run from the repository root.
python3 - <<'PY'
import publish

token = publish.create_account(1, "Example advertiser")["access_token"]
image = publish.upload_image({"owner_id": 1, "access_token": token,
                              "file": "file_handle_store/sample.jpg"})
result = publish.publish_ad({
    "creator_id": 1,
    "access_token": token,
    "text": "Discover our new collection",
    "image_hash": image["hash"],
})
print(result)
print(publish.get_ads(result["id"]))
print(publish.get_ads(result["id"], ["text", "image_hash"]))
PY
```

## API

| Function | Behavior |
| --- | --- |
| `create_account(account_id, name)` | Create an account. Returns `{"id": ..., "access_token": ...}`, or `002` for `account_id` if the ID is taken. |
| `create_account(account_id, name, access_token)` | Rename an existing account. Returns `{"id": ...}` or an error. |
| `get_account(account_id)` | Return `id` and `name`, `None` if missing/invalid, or a `003` error if storage fails. |
| `publish_ad(data)` | Require an existing `creator_id`, its `access_token`, one `text` field, and exactly one of `image` (link), `image_hash`, or `image_name` (image ad), or `video_id` (video ad). Return a numeric ad ID or an error. |
| `upload_image(data)` | Require an existing `owner_id`, its `access_token`, and a local image `file` path ending in `.jpg`, `.jpeg`, `.png`, `.gif`, or `.webp`; `image_name` is optional. Return `{"hash": ...}` or an error. |
| `list_images(owner_id, access_token, limit=None, cursor=None, since=None, until=None)` | Return one page of the images the account owns, newest first, with `image_url`, `width`, and `height`: `{"data": [...], "next_cursor": ...}`. A storage failure returns `003`. |
| `get_ads(ad_id, fields=None)` | Return the stored ad or requested fields (a list, or a comma-separated string such as `"text, image_hash"`); unknown fields return an error, and a storage failure returns `003`. |
| `list_ads(creator_id, limit=None, cursor=None, since=None, until=None)` | Return one page of the account's ads, newest first: `{"data": [...], "next_cursor": ...}`. Ads are public, so no token is needed; a storage failure returns `003`. |
| `get_ad(ad_id)` | Return the full stored ad, `None` if missing/invalid, or a `003` error if storage fails. |
| `upload_video(data)` | Require an existing `owner_id`, its `access_token`, a nonblank `name`, and a local video `file` path. Return a numeric video ID or an error. |
| `get_video(video_id, access_token)` | Return the video to its owner. An invalid ID, a missing video, and a wrong token all return `002` for `video_id`; a storage failure returns `003`. |
| `rename_video(video_id, name, access_token)` | Change a video's name with its owner's token. Return `{"id": ...}`, or an error for a missing video, wrong token, or blank name. |
| `list_videos(owner_id, access_token, limit=None, cursor=None, since=None, until=None)` | Return one page of the videos the account owns, newest first: `{"data": [...], "next_cursor": ...}`. `access_token` is the second positional argument in this function and in `AdsService.list_videos`, so callers that passed `limit` positionally must update. |

## Authentication

Every write to account-owned data needs the account's access token: renaming the
account, publishing, and uploading images or videos. Listing an account's images
or videos needs it too, since those reveal what the account owns; other reads
(accounts and ads) need no token.
`create_account` returns the token once; only its SHA-256 hash is stored, so a lost
token cannot be recovered. A missing or wrong token returns `002` for
`access_token`. Validation authenticates before checking image or video ownership,
so an unauthenticated caller cannot learn what an account owns. `get_video` and
`rename_video` return `002` for `video_id` for a wrong token, the same as for a
missing video.

Accounts created before authentication have no token and cannot write until an
operator issues one with `AdsService(repository).issue_access_token(account_id)`.
That call also replaces a lost or leaked token; the old one stops working.

## Objects and fields

An ad contains `id`, `system_name` (`ad_<id>`), `image_hash`, `text`,
`creator_id`, `creation_time` (Unix seconds), and `video_id`. IDs (accounts,
ads, videos) are integers from 0 to 2^63 − 1, given as ints or digit strings;
anything else is invalid (`002`) for writes and missing (`None`) for reads,
including any account created with a negative ID by an earlier build. Image ads
have `video_id: None`; video ads have `image_hash: None`. Errors contain
`error_code` and `error_message`: `001` means an unknown/duplicate field, `002`
an invalid value, and `003` a storage failure while handling the named field or
resource (`ad`, `account`, `image`, or `video`); the same request may succeed
when retried. Reads return `None` only for records that are missing. Text must
contain at least three characters and must not look like a URL or a hash (16–64
hex digits, such as an image hash); long words like `Internationalization` are
fine.

An image is identified by the first 32 hex characters of its content's SHA-256
(URLs: of the URL). Accounts can only publish images they own. An account owns an
image after uploading it with `upload_image` or after publishing with an `image`
link, which ingests it. Identical bytes get the same hash and are stored once, but each
account has to upload them itself to own them.

An image ad names its image exactly one way; each field is validated before
anything is ingested or saved:

- `image`: an HTTP(S) link, ingested as metadata. Local files are not accepted;
  upload them with `upload_image`, then publish by hash or name.
- `image_hash`: a 16–64 character alphanumeric handle that `creator_id` owns and
  that the image store can resolve. Hashes returned by `upload_image` publish
  unchanged; surrounding spaces are trimmed, but case and every other character
  must match.
- `image_name`: the name `creator_id` gave an image at upload, matched exactly
  after trimming. It resolves to that image's hash, which must also be in the
  image store.

Invalid values return `002` for that field. Missing, unowned, and unresolvable
images are indistinguishable, and authentication runs first. Supplying more than
one image field returns `002` for the second one, in `image`, `image_hash`,
`image_name` order.

Image files are limited to 30 MiB (Meta's ad image maximum) and videos to 1 GiB.
Larger files return `002` for `file` and leave nothing stored. The limit is enforced while copying, not from a size checked
in advance.

`video_id` must name a video owned by the ad's `creator_id`. A missing video and
another account's video both return `002` for `video_id`, so callers cannot tell
which IDs exist. Combining `video_id` with any image field also returns `002` for
`video_id`.

A video contains `id`, `name`, `owner_id` (the owning ad account), `upload_time`
(Unix seconds), and `video_hash`. Only the name can change; the other fields are
fixed at upload. `file` must be an existing local file ending in `.mp4`, `.mov`,
`.m4v`, `.webm`, `.mkv`, or `.avi`; the extension is the only format check. Video
errors use `owner_id`, `name`, `file`, or `video_id` as the field.

An owned image contains `hash`, `owner_id`, `creation_time`, `image_name`, and,
when listed, `image_url`, `width`, and `height`.
`creation_time` is when that account first uploaded or ingested it (Unix seconds);
re-uploading keeps the original time. `image_name` is the account's own label, or
`None`: it is trimmed and must not be blank, otherwise upload returns `002` for
`image_name`. Names are unique within an account (unnamed images are exempt);
uploading a different image under a name the account already uses returns `002`
for `image_name`, and the new image is not owned (its bytes stay stored).
Re-uploading with a name replaces the account's name; without one, the name is
kept. Images ingested by `publish_ad` have no name. Accounts that upload the same
bytes share the hash but keep separate names.

Listed images also carry `image_url`, `width`, and `height` from image storage.
`image_url` is the link a `publish_ad` ingestion came from, or `None` for
uploads, which have no hosted URL. `width` and `height` are the recorded pixel
size (seeded images and uploads with a recognized header). Images created before
2026-10-30T01:00:00Z (`service.DIMENSIONS_CUTOFF`), when uploads began recording
sizes, report a 1080×1080 placeholder if no size is stored, as do uploads
registered by older code at any time (their entries have no size fields); newer
images with no known size, such as link ingestions, report `None`. The cutoff
compares each account's own `creation_time`, so accounts sharing an image can
differ.

`list_ads`, `list_videos`, and `list_images` page by cursor. Pass the previous response's `next_cursor` to get
the next page; it is `None` on the last page. Treat cursors as opaque. Pages
never repeat or skip items when new ones arrive between requests. `limit`
defaults to 25 (maximum 100). `since` (inclusive) and `until` (exclusive) filter
by `creation_time` (ads and images) or `upload_time` (videos) in Unix seconds and
can be combined with a cursor. Ties are ordered by ad ID, video ID, or image hash,
descending. Invalid parameters return `002` for `creator_id` (ads), `owner_id`,
`limit`, `cursor`, `since`, or `until`; a wrong token returns `002` for
`access_token`.

## Code structure

`AdAccount` and `Ad` are immutable dataclasses with named fields and `to_dict()`
methods. `AdDraft` represents validated input before publication assigns an ID
and metadata. The existing functions in `publish.py` still return dictionaries.

| Module | Responsibility |
| --- | --- |
| `config.py` | `DATA_DIR`, the data directory used by `publish`, `image_store`, and `video_store`. |
| `models.py` | `AdAccount`, `AdDraft`, `Ad`, `Image`, and `Video` objects. |
| `repository.py` | `AdsRepository` storage interface and SQLite implementation, including token hashes and image ownership. |
| `validation.py` | Account, ad input, upload, and read projection validation; token checks. |
| `publishing.py` | `AdPublisher`: validate input, ingest and claim the image, persist the ad. |
| `service.py` | `AdsService` object API for accounts, ads, and image and video uploads and listings. |
| `file_store.py` | Size-limited copy of a local file into storage, named by content hash. |
| `video_store.py` | Local video storage on top of `file_store`. |
| `publish.py` | Compatibility API that translates objects and errors into dictionaries. |

Use the object API to work directly with accounts and ads:

```python
import publish
from repository import SQLiteAdsRepository
from service import AdsService

repository = SQLiteAdsRepository(publish.DB_PATH)
service = AdsService(repository)
account, token = service.create_account(1, "Example advertiser")
ad = service.publish_ad({
    "creator_id": account.id,
    "access_token": token,
    "text": "Discover our new collection",
    "image": "https://cdn.example.com/ads/img1.jpg",
})
print(ad.id, ad.text, ad.image_hash)
print(service.get_ad(ad.id).to_dict())
```

The object API raises `ValidationError` for invalid input, and its subclass
`StorageError` (`003`) when a lookup made during validation, or image or video
ingestion, fails; an unreadable upload source is invalid input (`002` for
`file`); other storage failures propagate. The compatibility functions turn
every failure into an error dictionary. The schema is versioned with SQLite's
`user_version`: creating a repository on an older database migrates it once,
under a write lock, and an up-to-date database is opened without one. Older
databases gain the `videos` and `image_owners` tables, the `ads.video_id` and
`accounts.token_hash` columns, and an index for listing a creator's ads;
existing ads read back with `video_id: None`, and existing accounts have no
token.

## Extending validation and publishing

Add validation rules that receive an `AdDraft` and raise `ValidationError` to
reject it. All validation runs before image ingestion or ad persistence:

```python
from publishing import AdPublisher
from validation import AdValidator, ValidationError

def limit_text_length(draft):
    if len(draft.text) > 120:
        raise ValidationError("002", "text")

validator = AdValidator(repository, rules=[limit_text_length])  # image_storage=... if custom
publisher = AdPublisher(repository, validator=validator)
service = AdsService(repository, publisher=publisher)
```

For different field policies, subclass `AdValidator` and override its
`validate_creator`, `validate_text`, `validate_image`, `validate_hash(value, creator_id)`, or `validate_image_name(value, creator_id)` methods,
or supply a validator with a `validate(data) -> AdDraft` method. Account and
projection validators are separate classes as well. Upload rules work the same
way: subclass `UploadValidator` (images) or `VideoValidator` and pass them as
`AdsService(repository, upload_validator=..., video_validator=...)`; they run
before anything is stored and also check listing parameters (`validate_query`).

`AdPublisher` accepts an `image_storage` object with `store_image(input)` and
`get_image_by_hash(hash)`, and a `clock` callable, read once per publish and
stored on the draft as `creation_time` so the ingested image and the ad share a
timestamp. Its default validator checks hashes against that same storage; pass
the same `image_storage` to a custom `AdValidator`. Subclass it to customize
`normalize_image(draft)` or `persist(draft)`, or supply another publisher with
`publish(data) -> Ad` to `AdsService`.
`AdsService(repository, image_storage=..., clock=...)` stores and timestamps
uploads with those and builds its default publisher on them; a publisher you supply should use the same
storage. A different repository must implement the `AdsRepository` protocol in
`repository.py` using the same domain objects.

## Storage

By default, generated data lives beside the modules under `data/`:

- `ads.db`: SQLite accounts, ads, videos, and image ownership.
- `image_store_data/`: copied local files, named by content hash, and
  `persisted_images.json`. Identical bytes are kept once, under the suffix of
  the first upload to be registered. Registered paths are absolute: a copied data
  directory keeps its local copy on re-upload, but its registry entries still
  name the original location.
- `videos/`: uploaded video files, named by the first 32 hex characters of their
  SHA-256. Identical uploads share one file.

Set `ADS_STACK_DATA_DIR` before importing the modules to use another location:

```bash
export ADS_STACK_DATA_DIR="$HOME/my-ads-data"
```

Paths are independent of the current working directory. `images.json` supplies
three example image records; it is separate from generated data. Without the
file there are no seeded images; if it cannot be read or parsed, image lookups and
ingestion fail with `003` rather than dropping the seeds.

## Image store behavior

`image_store.py` is a simulated image registry. URLs are recorded as metadata
with a hash derived from the URL; image bytes are **not downloaded**. Local files
are copied into storage and hashed by their contents. If `persisted_images.json`
cannot be read or parsed, lookups and ingestion fail with `003` instead of treating
the registry as empty, so a damaged registry is never overwritten. Registry
writes go to a synced temporary file that replaces the registry in one rename, so
an interrupted write leaves the previous registry intact. Writers hold an
exclusive file lock (`image_registry.lock` in the data directory, Unix only)
while they reload, append, and save, so concurrent ingestions never lose records.
Uploaded files record `width` and `height` read from their PNG, GIF, JPEG, or
WEBP header (`None` when the header is missing or unrecognized); URL entries
record `0` because nothing is downloaded. The supplied `sample.jpg` is a text
placeholder, not a decodable JPEG, so it records `None`.
`upload_image` checks the extension only (case-insensitive) and never
validates image contents; `image_store` itself accepts any file.

## Tests

Run the local business behavior tests with Python's standard library:

```bash
# Run from the repository root.
python3 -m unittest discover -s tests -v
```

The tests use temporary storage and include fresh-process persistence checks.
They are adapted from the original task's behavior tests for the canonical API;
benchmark isolation, API discovery, and reward reporting are omitted.
Additional checks cover the object API, custom rules, custom publishing stages,
injected image storage and clocks, and compatibility with existing SQLite data.
The suite has 104 tests, including boundary cases, storage failures, rollback,
schema migrations, concurrent SQLite ad ID allocation and registry writes, token
and ownership enforcement, size limits, and ad, video, and image listings. See
[VERIFICATION.md](VERIFICATION.md) for the test audit, deliberate regression
checks, and remaining limits.

## Source

Extracted from `~/clarerz-tbench/simple-ads-stack`: the completed
`solution/publish.py`, the existing image store, seed metadata, and sample fixture.
Storage paths now work locally; the API uses canonical field names and omits
grader-discovery aliases. This directory contains no Dockerfile, task prompt,
task metadata, verifier, oracle installer, or benchmark results.
