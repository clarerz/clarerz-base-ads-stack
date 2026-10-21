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

## Source

Extracted from `~/clarerz-tbench/simple-ads-stack`: the completed
`solution/publish.py`, the existing image store, seed metadata, and sample fixture.
Storage paths now work locally; the API uses canonical field names and omits
grader-discovery aliases. This directory contains no Dockerfile, task prompt,
task metadata, verifier, oracle installer, or benchmark results.
