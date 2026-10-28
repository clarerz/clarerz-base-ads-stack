# Simple Ads Stack

A small Python base for authenticated ad accounts, image and video uploads, ad
publishing, and persistent reads. Uses only the Python standard library; no installation or server is needed.

## Quick start

```bash
cd ~/clarerz-base-ads-stack
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
| `get_account(account_id)` | Return `id` and `name`, or `None` if missing/invalid. |
| `publish_ad(data)` | Require an existing `creator_id`, its `access_token`, one `text` field, and exactly one of `image` (link), `image_hash`, or `image_name` (image ad), or `video_id` (video ad). Return a numeric ad ID or an error. |
| `upload_image(data)` | Require an existing `owner_id`, its `access_token`, and a local image `file` path; `image_name` is optional. Return `{"hash": ...}` or an error. |
| `list_images(owner_id, access_token, limit=None, cursor=None, since=None, until=None)` | Return one page of the images the account owns, newest first: `{"data": [...], "next_cursor": ...}`. |
| `get_ads(ad_id, fields=None)` | Return the stored ad or requested fields; unknown fields return an error. |
| `get_ad(ad_id)` | Return the full stored ad, or `None` if missing/invalid. |
| `upload_video(data)` | Require an existing `owner_id`, its `access_token`, a nonblank `name`, and a local video `file` path. Return a numeric video ID or an error. |
| `rename_video(video_id, name, access_token)` | Change a video's name with its owner's token. Return `{"id": ...}`, or an error for a missing video, wrong token, or blank name. |
| `list_videos(owner_id, limit=None, cursor=None, since=None, until=None)` | Return one page of the account's videos, newest first: `{"data": [...], "next_cursor": ...}`. |

## Authentication

Every write to account-owned data needs the account's access token: renaming the
account, publishing, and uploading images or videos. Listing an account's images
needs it too; other reads need no token.
`create_account` returns the token once; only its SHA-256 hash is stored, so a lost
token cannot be recovered. A missing or wrong token returns `002` for
`access_token`. Validation authenticates before checking image or video ownership,
so an unauthenticated caller cannot learn what an account owns. `rename_video`
returns `002` for `video_id` for a wrong token, the same as for a missing video.

Accounts created before authentication have no token and cannot write until an
operator issues one with `AdsService(repository).issue_access_token(account_id)`.
That call also replaces a lost or leaked token; the old one stops working.

## Objects and fields

An ad contains `id`, `system_name` (`ad_<id>`), `image_hash`, `text`, `creator_id`,
`creation_time` (Unix seconds), and `video_id`. Image ads have `video_id: None`;
video ads have `image_hash: None`. Errors contain `error_code` and
`error_message`: `001` means an unknown/duplicate field, and `002` means an invalid
value or a storage failure. Text must contain at least three characters and must
not look like a URL or a hash.

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

An owned image contains `hash`, `owner_id`, `creation_time`, and `image_name`.
`creation_time` is when that account first uploaded or ingested it (Unix seconds);
re-uploading keeps the original time. `image_name` is the account's own label, or
`None`: it is trimmed and must not be blank, otherwise upload returns `002` for
`image_name`. Names are unique within an account (unnamed images are exempt);
uploading a different image under a name the account already uses returns `002`
for `image_name`, and the new image is not owned (its bytes stay stored).
Re-uploading with a name replaces the account's name; without one, the name is
kept. Images ingested by `publish_ad` have no name. Accounts that upload the same
bytes share the hash but keep separate names.

`list_videos` and `list_images` page by cursor. Pass the previous response's `next_cursor` to get
the next page; it is `None` on the last page. Treat cursors as opaque. Pages
never repeat or skip videos when new uploads arrive between requests. `limit`
defaults to 25 (maximum 100). `since` (inclusive) and `until` (exclusive) filter
by `upload_time` (videos) or `creation_time` (images) in Unix seconds and can be
combined with a cursor. Ties are ordered by video ID or image hash, descending.
Invalid parameters return `002` for `owner_id`, `limit`, `cursor`, `since`, or
`until`; a wrong token for `list_images` returns `002` for `access_token`.

## Code structure

`AdAccount` and `Ad` are immutable dataclasses with named fields and `to_dict()`
methods. `AdDraft` represents validated input before publication assigns an ID
and metadata. The existing functions in `publish.py` still return dictionaries.

| Module | Responsibility |
| --- | --- |
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

The object API raises `ValidationError` for invalid input and propagates storage
failures. The compatibility functions preserve their error dictionaries and
missing-record behavior. Older databases gain the `videos` and `image_owners`
tables and the `ads.video_id` and `accounts.token_hash` columns automatically;
existing ads read back with `video_id: None`, and existing accounts have no token.

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
projection validators are separate classes as well.

`AdPublisher` accepts an `image_storage` object with `store_image(input)` and
`get_image_by_hash(hash)`, and a `clock` callable. Its default validator checks
hashes against that same storage; pass the same `image_storage` to a custom
`AdValidator`. Subclass it to customize `normalize_image(draft)` or
`persist(draft)`, or supply another publisher with `publish(data) -> Ad` to
`AdsService`. A different repository must implement the `AdsRepository` protocol in
`repository.py` using the same domain objects.

## Storage

By default, generated data lives beside the modules under `data/`:

- `ads.db`: SQLite accounts and ads.
- `image_store_data/`: copied local files, named by content hash, and
  `persisted_images.json`.
- `videos/`: uploaded video files, named by the first 32 hex characters of their
  SHA-256. Identical uploads share one file.

Set `ADS_STACK_DATA_DIR` before importing the modules to use another location:

```bash
export ADS_STACK_DATA_DIR="$HOME/my-ads-data"
```

Paths are independent of the current working directory. `images.json` supplies
three example image records; it is separate from generated data.

## Image store behavior

`image_store.py` is a simulated image registry. URLs are recorded as metadata
with a hash derived from the URL; image bytes are **not downloaded**. Local files
are copied into storage and hashed by their contents. If `persisted_images.json`
cannot be read or parsed, lookups and ingestion fail with `002` instead of treating
the registry as empty, so a damaged registry is never overwritten. Registry
writes go to a synced temporary file that replaces the registry in one rename, so
an interrupted write leaves the previous registry intact. The supplied `sample.jpg`
is a text placeholder, not a decodable JPEG. File ingestion does not validate
image formats.

## Tests

Run the local business behavior tests with Python's standard library:

```bash
cd ~/clarerz-base-ads-stack
python3 -m unittest discover -s tests -v
```

The tests use temporary storage and include fresh-process persistence checks.
They are adapted from the original task's behavior tests for the canonical API;
benchmark isolation, API discovery, and reward reporting are omitted.
Additional checks cover the object API, custom rules, custom publishing stages,
injected image storage and clocks, and compatibility with existing SQLite data.
The suite has 76 tests, including boundary cases, storage failures, rollback, and
concurrent SQLite ad ID allocation, token and ownership enforcement, size limits, and image listing. See [VERIFICATION.md](VERIFICATION.md) for
the test audit, deliberate regression checks, bugs fixed, and remaining limits.

## Source

Extracted from `~/clarerz-tbench/simple-ads-stack`: the completed
`solution/publish.py`, the existing image store, seed metadata, and sample fixture.
Storage paths now work locally; the API uses canonical field names and omits
grader-discovery aliases. This directory contains no Dockerfile, task prompt,
task metadata, verifier, oracle installer, or benchmark results.
