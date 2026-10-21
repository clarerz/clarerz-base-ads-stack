# Simple Ads Stack

A small Python base for ad accounts, single-image ad publishing, and persistent
reads. Uses only the Python standard library; no installation or server is needed.

## Quick start

```bash
cd ~/clarerz-base-ads-stack
python3 - <<'PY'
import publish

print(publish.create_account(1, "Example advertiser"))
result = publish.publish_ad({
    "creator_id": 1,
    "text": "Discover our new collection",
    "image": "sample.jpg",
})
print(result)
print(publish.get_ads(result["id"]))
print(publish.get_ads(result["id"], ["text", "image_hash"]))
PY
```

## API

| Function | Behavior |
| --- | --- |
| `create_account(account_id, name)` | Persist an account; an existing ID replaces its name. Returns `{"id": ...}` or an error. |
| `get_account(account_id)` | Return `id` and `name`, or `None` if missing/invalid. |
| `publish_ad(data)` | Require an existing `creator_id`, one `text` field, and `image` or `image_hash`. Return a numeric ad ID or an error. |
| `get_ads(ad_id, fields=None)` | Return the stored ad or requested fields; unknown fields return an error. |
| `get_ad(ad_id)` | Return the full stored ad, or `None` if missing/invalid. |

An ad contains `id`, `system_name` (`ad_<id>`), `image_hash`, `text`, `creator_id`,
and `creation_time` (Unix seconds). Errors contain `error_code` and
`error_message`: `001` means an unknown/duplicate field, and `002` means an invalid
value or a storage failure. Text must contain at least three characters and must
not look like a URL or a hash.

`image` accepts an HTTP(S) URL, a local path, or a filename from
`file_handle_store/`. `image_hash` accepts a 16–64 character alphanumeric handle;
the existing implementation does not check that the handle exists. If both image
fields are supplied, the ingested image's hash takes precedence.

## Objects and code structure

`AdAccount` and `Ad` are immutable dataclasses with named fields and `to_dict()`
methods. `AdDraft` represents validated input before publication assigns an ID
and metadata. The existing functions in `publish.py` still return dictionaries.

| Module | Responsibility |
| --- | --- |
| `models.py` | `AdAccount`, `AdDraft`, and `Ad` objects. |
| `repository.py` | `AdsRepository` storage interface and SQLite implementation. |
| `validation.py` | Account, ad input, and read projection validation. |
| `publishing.py` | `AdPublisher`: validate input, normalize the image, persist the ad. |
| `service.py` | `AdsService` object API for accounts and ads. |
| `publish.py` | Compatibility API that translates objects and errors into dictionaries. |

Use the object API to work directly with accounts and ads:

```python
import publish
from repository import SQLiteAdsRepository
from service import AdsService

repository = SQLiteAdsRepository(publish.DB_PATH)
service = AdsService(repository)
account = service.create_account(1, "Example advertiser")
ad = service.publish_ad({
    "creator_id": account.id,
    "text": "Discover our new collection",
    "image": "sample.jpg",
})
print(ad.id, ad.text, ad.image_hash)
print(service.get_ad(ad.id).to_dict())
```

The object API raises `ValidationError` for invalid input and propagates storage
failures. The compatibility functions preserve their error dictionaries and
missing-record behavior. The SQLite schema is unchanged, so existing data works
with either API.

## Extending validation and publishing

Add validation rules that receive an `AdDraft` and raise `ValidationError` to
reject it. All validation runs before image ingestion or ad persistence:

```python
from publishing import AdPublisher
from validation import AdValidator, ValidationError

def limit_text_length(draft):
    if len(draft.text) > 120:
        raise ValidationError("002", "text")

validator = AdValidator(repository, rules=[limit_text_length])
publisher = AdPublisher(repository, validator=validator)
service = AdsService(repository, publisher=publisher)
```

For different field policies, subclass `AdValidator` and override its
`validate_creator`, `validate_text`, `validate_image`, or `validate_hash` methods,
or supply a validator with a `validate(data) -> AdDraft` method. Account and
projection validators are separate classes as well.

`AdPublisher` accepts an `image_storage` object with `store_image(input)` and a
`clock` callable. Subclass it to customize `normalize_image(draft)` or
`persist(draft)`, or supply another publisher with `publish(data) -> Ad` to
`AdsService`. A different repository can implement `save_account`, `get_account`,
`save_ad`, and `get_ad` using the same domain objects.

## Storage

By default, generated data lives beside the modules under `data/`:

- `ads.db`: SQLite accounts and ads.
- `image_store_data/`: copied local files and `persisted_images.json`.

Set `ADS_STACK_DATA_DIR` before importing the modules to use another location:

```bash
export ADS_STACK_DATA_DIR="$HOME/my-ads-data"
```

Paths are independent of the current working directory. `images.json` supplies
three example image records; it is separate from generated data.

## Image store behavior

`image_store.py` is a simulated image registry. URLs are recorded as metadata
with a hash derived from the URL; image bytes are **not downloaded**. Local files
are copied into storage and hashed by their contents. The supplied `sample.jpg`
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
The suite has 44 tests, including boundary cases, storage failures, rollback, and
concurrent SQLite ad ID allocation. See [VERIFICATION.md](VERIFICATION.md) for
the test audit, deliberate regression checks, bugs fixed, and remaining limits.

## Source

Extracted from `~/clarerz-tbench/simple-ads-stack`: the completed
`solution/publish.py`, the existing image store, seed metadata, and sample fixture.
Storage paths now work locally; the API uses canonical field names and omits
grader-discovery aliases. This directory contains no Dockerfile, task prompt,
task metadata, verifier, oracle installer, or benchmark results.
