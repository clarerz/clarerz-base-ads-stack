# Simple Ads Stack

A small Python base for authenticated ad accounts, image and video uploads, ad
publishing, and persistent reads. The core library uses only the Python standard
library and needs no installation; the optional HTTP API needs the packages in
`requirements.txt`.

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
| `get_image(owner_id, access_token, image_hash=None, image_name=None)` | Return one image the account owns, found by exactly one of `image_hash` or `image_name` (trimmed; matched exactly), with the same fields as `list_images`. An unknown image and another account's image both return `002` for the field used; giving both returns `002` for `image_name`, neither `002` for `image_hash`. Authentication runs first; a storage failure returns `003`. |
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
That call also replaces a lost or leaked token; the old one stops working, and so
do the account's web login and its sessions (see [Web UI](#web-ui)).

An account can also have one web login (username and password) for the web UI.
`AdsService.create_user(account_id, access_token, username, password)` creates it;
`login(login, password)` returns a session token that `authenticate` accepts for that
account wherever an access token is accepted, until it expires or `logout` ends it.

## HTTP API

`api.py` serves the same objects over HTTP with FastAPI. Install its packages
into a virtual environment and start it with an admin key:

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
ADS_ADMIN_KEY="<secret>" ADS_MEDIA_KEY="<another secret>" .venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000
```

Interactive docs are served at `/docs`. Every secret travels as
`Authorization: Bearer <secret>`; tokens are never read from bodies or query
strings. `POST /accounts` takes the admin key from `ADS_ADMIN_KEY`; without one,
no account can be created. Every other authenticated route takes the account's
access token, and each of them also accepts the session cookie with `X-Ads-UI: 1`,
except `POST /accounts/{account_id}/users`, which only the token opens. JSON bodies hold the same fields as the Python API, and errors
use the same `{"error_code", "error_message"}` body.

| Method | Path | Auth | Success |
| --- | --- | --- | --- |
| `POST` | `/accounts` (`{"id", "name"}`) | Admin key | 201 `{"id", "access_token"}` |
| `GET` | `/accounts/{account_id}` | None | 200 `{"id", "name"}` |
| `PATCH` | `/accounts/{account_id}` (`{"name"}`) | Account token | 200 `{"id", "name"}` |
| `POST` | `/accounts/{account_id}/ads` (`text` plus one image field or `video_id`) | Account token | 201 `{"id"}` |
| `PATCH` | `/accounts/{account_id}/ads/{ad_id}` (`text`, and `video_id` or one image field) | Account token | 200 ad |
| `GET` | `/accounts/{account_id}/ads?limit&cursor&since&until` | None | 200 `{"data", "next_cursor"}` |
| `GET` | `/ads/{ad_id}?fields=text,image_hash` | None | 200 ad or projected fields |
| `POST` | `/accounts/{account_id}/images?filename=&image_name=` (raw bytes) | Account token | 201 `{"hash"}` |
| `GET` | `/accounts/{account_id}/images?limit&cursor&since&until` | Account token | 200 `{"data", "next_cursor"}` |
| `GET` | `/accounts/{account_id}/images/lookup?image_hash=` or `?image_name=` | Account token | 200 image |
| `POST` | `/accounts/{account_id}/videos?filename=&name=` (raw bytes) | Account token | 201 `{"id"}` |
| `GET` | `/accounts/{account_id}/videos?limit&cursor&since&until` | Account token | 200 `{"data", "next_cursor"}` |
| `GET` | `/videos/{video_id}` | Account token | 200 video |
| `PATCH` | `/videos/{video_id}` (`{"name"}`) | Account token | 200 video |
| `GET` | `/accounts/{account_id}/media/{images\|videos}/{hash}?exp&sig` | Account token or signed URL | 200 file bytes |
| `GET` | `/accounts/{account_id}/ads/{ad_id}/previews?ad_format=` | Account token | 200 `{"data": [{"format", "preview_url", "width", "height", "text", "warnings"}], "media": {"kind", "url"}, "share_url", "share_expires"}` |
| `GET` | `/accounts/{account_id}/ads/{ad_id}/previews/{feed\|story\|banner}?exp&sig` | Account token or signed URL | 200 HTML page |
| `GET` | `/accounts/{account_id}/ads/{ad_id}/share?exp&sig` | Account token or signed URL | 200 HTML page with all three placements |
| `POST` | `/accounts/{account_id}/users` (`{"username", "password"}`) | Account token, `X-Ads-UI: 1` | 201 `{"username", "account_id"}` |
| `POST` | `/session` (`{"login", "password"}`) | `X-Ads-UI: 1` | 200 `{"username", "account_id", "account_name"}`, sets `ads_session` |
| `GET` | `/session` | Session cookie | 200 same body |
| `DELETE` | `/session` | `X-Ads-UI: 1` | 204, clears `ads_session` |
| `GET` | `/ui`, `/ui/app.js`, `/ui/app.css` | None | 200 web UI files |

Status codes: `001` and `002` errors return 400, except `002` for
`access_token` (a missing or wrong token or admin key), which returns 401. A
body that is not a JSON object returns 400 with `002` for `body`. An unknown path
returns 404 with `002` for `path`, and an unsupported method 405 with `002` for
`method`. Reads of missing records return 404 with `002` for the ID field
(`account_id`, `ad_id`).
`/videos/{video_id}` returns the same 404 with `002` for `video_id` for a missing
video and for a missing or wrong token, so callers cannot probe video IDs. Storage
failures (`003`) return 503.

### Previews

Every image and video the HTTP API returns to its owner (listings, the image
lookup, and the video routes) carries a `preview_url`, so a page can show it with
`<img>` or `<video>`, which cannot send a bearer token. For uploads it is a
signed link to the stored bytes under the owning account's
`/accounts/{account_id}/media` path, valid for 10 minutes (`api.PREVIEW_SECONDS`);
fetch the JSON again for a fresh one. Server-side clients can instead fetch that
path with the account's bearer token and no signature; a wrong token returns 401. Link-ingested images have no stored bytes,
so their `preview_url` is their source link (`image_url`). Without
`ADS_MEDIA_KEY`, `preview_url` is `null` and no media is served; use a key
different from the admin key, shared by every server process.

The signature is an HMAC-SHA256 over the account, kind, content hash, expiry, and
the account's current token hash, so a URL opens only that account's file until it
expires and cannot be extended or moved to other content or another account. It is
not the account token and grants nothing else. Anyone holding the URL can fetch the
file until then; issuing the account a new token revokes all its outstanding URLs.
Every request, signed or with a token, checks that the account still owns the
image or video, so content another account uploaded is not served even when the
stored bytes are identical. A missing, expired, altered, or revoked signature, media
the account does not own, and a missing file all return 404 with `002` for `media`. `/media` supports `Range` requests, so videos stream, and sets
`Cache-Control: private, max-age=<seconds left, at most 600>, immutable`,
`X-Content-Type-Options: nosniff`, and `Content-Security-Policy: sandbox`. Files
are stored without extensions, so the content type comes from their leading
bytes (PNG, GIF, JPEG, WEBP, MP4, MOV, WebM, Matroska, AVI); anything else, such
as the placeholder `sample.jpg`, is served as `application/octet-stream`. URLs are
absolute, built from the request's host; behind a proxy, run uvicorn with
`--proxy-headers` so they name the public host.

### Ad previews

`GET /accounts/{account_id}/ads/{ad_id}/previews` shows how one of the account's ads
renders in each format. It returns data, not markup: each entry's `preview_url` is a
signed link to a static HTML page, and a client embeds it with
`<iframe src="{preview_url}" width="{width}" height="{height}" title="Ad preview">`.
`?ad_format=` returns one format. Nothing is rendered until the page is fetched.
Each entry's `text` is the text as that format displays it (cut like the page, not
HTML-escaped), and the top-level `media` is `{"kind": "image" | "video", "url"}`: a link
image's source link, or a signed URL valid as long as the `preview_url`s, and `null` when
the bytes cannot be shown or there is no `ADS_MEDIA_KEY`.

| Format | Page (CSS px) | Media frame | Text |
| --- | --- | --- | --- |
| `feed` | 500×640 | 500×500 (1:1), below the account name and text | Cut at 125 characters with "See more" |
| `story` | 360×640 | Full page (9:16); text over a gradient, clear of the top and bottom safe zones | Cut at 125 characters |
| `banner` | 600×354 | 600×314 (1.91:1), above one line of text | Cut at 40 characters |

Media fills its frame and is cropped to fit. Videos play muted and have controls; the
story also autoplays and loops, and the others show the first frame. `warnings` lists
`text_truncated` when the ad's text is cut and `media_cropped` when the image's known
size is more than 1% away from the frame's aspect ratio; video sizes are unknown, so
videos never report it.

The listing needs the account's token, like image and video listings: previews link
the account's stored bytes, which are otherwise served only to the owner. An unknown
account returns 400 with `002` for `creator_id`; another account's ad and a missing ad
both return 404 with `002` for `ad_id`; an unknown format returns 400 with `002` for
`ad_format`.

Preview URLs use the same signing as media URLs, scoped to one ad in one format, and
expire after `api.PREVIEW_SECONDS`; issuing the account a new token revokes them.
Server-side clients can fetch the page path with the account's bearer token instead (a
wrong token, or a token sent with another scheme than `Bearer`, returns 401; an account
with no issued token cannot preview). A missing, expired, altered, revoked, or reused signature,
an unknown format, and an ad the account does not own all return 404 with `002` for
`preview`. Without `ADS_MEDIA_KEY`, `preview_url` is `null`, and pages fetched with the
token show stored media as unavailable.

The page is static HTML that runs no scripts, with every ad and account value
HTML-escaped. Uploaded images and videos appear through media URLs signed when the page
is rendered, link images through their link. Those media URLs expire with the preview
URL that opened the page, so a preview URL grants nothing past its own expiry. Media that cannot be shown (no stored
bytes a browser can display, such as the `sample.jpg` placeholder, or bytes the account
does not own) appears as a labeled "Media unavailable" box. Pages are served with
`Content-Security-Policy: sandbox; default-src 'none'; img-src http: https:; media-src
<this server>; style-src 'unsafe-inline'`, `Referrer-Policy: no-referrer` (the page URL
carries its signature), `X-Content-Type-Options: nosniff`, and `Cache-Control:
no-store`. Ads are previewed by ID only; ads have no names.

**Share links.** The listing also returns top-level `share_url` and `share_expires` (Unix
seconds), both `null` without `ADS_MEDIA_KEY`. `share_url` opens
`GET /accounts/{account_id}/ads/{ad_id}/share`, a static HTML page that frames the feed,
story, and banner pages, so one link shows an ad in every placement to someone with no
account. It is valid for 7 days (`api.SHARE_SECONDS`), and the preview and media URLs it
embeds expire with it. The link is signed like a preview URL, so reissuing the account's
access token or changing `ADS_MEDIA_KEY` revokes it. Every invalid link (missing, expired,
altered, revoked, or for an ad the account does not own) gets the same HTML 404, "This
preview link is invalid or has expired.", so a link reveals nothing about why it failed.
A bearer token is also accepted on the route; the embedded URLs then last 10 minutes. On
an edited ad's page, "Last edited YYYY-MM-DD HH:MM UTC" appears under the heading, before
"Link expires". The page is served with `Content-Security-Policy: sandbox; default-src
'none'; frame-src <this server>; style-src 'unsafe-inline'; base-uri 'none'; form-action
'none'`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and
`Cache-Control: no-store`. Share links are stateless, so one link cannot be revoked on
its own.

The image lookup takes exactly one of `image_hash` or `image_name` and follows
`get_image` in the Python API; a missing image, another account's image, and a
malformed or absent lookup all return 404 with `002` for that field.

Uploading content the account already uploaded returns 409 with `004` for `file`
and names the existing asset, changing nothing (an image re-upload does not
rename it):

```json
{"error_code": "004", "error_message": "This file was already uploaded to this account",
 "hash": "<existing image hash>"}
```

Videos carry `"id"` instead of `"hash"`. Only byte-identical content counts, and
only the account's own uploads: an image ingested from a link does not, and
another account uploading the same bytes succeeds as usual, so the error reveals
nothing about other accounts' files. The Python API does not use `004`; it still
accepts re-uploads.

JSON bodies are limited to 64 KiB (`api.MAX_JSON_BYTES`) and must declare a
`Content-Length`. Larger bodies return 413 and chunked ones 411, both with `002`
for `body`, before authentication runs, so the server never buffers an
unauthenticated body of unbounded size.

The account in the path stands in for `creator_id` when publishing or editing, so
a body that names `creator_id` or `access_token` returns `001`. An unknown account in an
ad route is reported as `002` for `creator_id`, and in an upload or listing route
as `002` for `owner_id`.

`PATCH` on an ad changes only the fields sent: `text`, `video_id` (a video ad), or
one of `image`, `image_hash`, `image_name` (an image ad). An ad keeps its type, so
an image field on a video ad, or `video_id` on an image ad, returns `002` for that
field. Errors come in this order: body shape (`002` for `body` when empty or not an
object, `001` for an unknown field, `002` for `video_id` or a second image field
when sent together), then the account (`002` for `creator_id`), then the token
(401), then the ad. A missing ad, another account's ad, and a non-numeric `ad_id`
all return the same 404 with `002` for `ad_id`. The edit returns 200 with the ad,
whose `update_time` is the edit's Unix time.

Uploads send the file's bytes as the request body with a `Content-Length`
header. `filename` supplies only the extension (the same extensions as the
Python API); it never names a file on the server. The server checks the token,
`image_name` or the video `name`, the extension, and the declared length before reading the body,
spools it under `uploads/` in the data directory, and deletes the spooled copy
afterwards. A missing `Content-Length` (for example a chunked upload) returns
411 and a body over the size limit returns 413, both with `002` for `file`.

## Web UI

`GET /ui` serves a browser page for internal demos: sign in, upload images and
videos, create ads, and see the account's ads. Start the HTTP API as above and open
`http://127.0.0.1:8000/ui`. It uses the same routes as other clients; there is no
build step.

**Logins.** Each ad account has at most one web login. Create it once from the page
("Create a login") with the account ID, the account's access token, a username, and a
password; the token proves ownership for that one request and is not stored. Sign in
with the username (any case) or the numeric account ID. Usernames are 3–32 ASCII
letters, digits, `_`, `.`, or `-` with at least one letter (so they are never all
digits); passwords are 8–128 characters and may not equal the access token. Passwords
are stored as salted scrypt hashes. Errors: `002` for `username` (invalid or taken),
`password`, or `account_id` (the account already has a login) return 400; a missing or
wrong token returns 401 `002` for `access_token`. A session cookie never authorizes
sign-up.

**Sessions.** A successful `POST /session` sets the `ads_session` cookie (`HttpOnly`,
`SameSite=Strict`, `Path=/`, 12 hours). It is `Secure` unless the server runs with
`ADS_INSECURE_COOKIES=1`; browsers accept `Secure` cookies from `http://localhost` and
`http://127.0.0.1`, so set it only to serve plain HTTP on another host. Every failed
login (unknown login, wrong password, throttled, or a login whose token was reissued)
returns the same 401 `002` for `login`, and every attempt runs exactly one scrypt check
so timing does not tell them apart. After 5 consecutive failures a login is refused
until 30 seconds after the latest one. Issuing the account a new access token ends its
login and sessions; the new token can then create a login again.

**Cookie authentication.** The account routes accept the session cookie in place of
the bearer token only when the request has `X-Ads-UI: 1` and no `Authorization`
header. Cross-origin pages cannot send that header (the server answers no CORS
preflight), so other sites cannot use the cookie. `POST /accounts` and sign-up read the
`Authorization` header only. Responses to cookie-authenticated requests carry working
signed `preview_url`s, signed with the account's stored token hash. They expire after
`api.PREVIEW_SECONDS` (600 s); reissuing the access token revokes them, and signing out
does not.

**The page.** Images and videos are listed separately (25 per page) and either can be
selected as an ad's media, or an image link entered instead. The account's ads are
listed newest first, 10 per page. Upload progress, duplicate uploads (409), and API
errors are shown next to the relevant field; a 401 returns to sign-in. Pages are served
with a CSP that allows only this server's script, style, and connections, and server
values are inserted as text only. The exact policy is `default-src 'none'; script-src 'self';
style-src 'self'; connect-src 'self'; img-src 'self' http: https:; media-src 'self';
form-action 'self'; frame-ancestors 'none'; base-uri 'none'`.

**Previews.** Each ad row has a Preview button. It opens a dialog that draws the feed,
story, and banner placements directly (no frames, using `/ui/preview.css`, the same
stylesheet as the preview pages), each with its warnings ("Text is cut off", "Image is
cropped to fit"). Below them are the ad's share link, its expiry, and a Copy link button
(if the browser blocks copying, the link text is selected instead). Anyone with the link
can see the ad for 7 days, and signing out does not revoke it; see Share links above.
Without `ADS_MEDIA_KEY`, the placements show their text and "Media unavailable" for
media, and stay editable.

**Editing in the preview.** Ad text is outlined on hover or focus; click it, or focus it
and press Enter or Space, to edit. Blur or closing the dialog saves with
`PATCH /accounts/{id}/ads/{ad_id}`; Escape cancels, and unchanged text sends nothing. Text
the API rejects stays open with the error shown. To replace an image, click it (or its
"Replace image" overlay) and pick a file; a video has a separate "Replace video" button
in its top-right corner, so play and pause stay native. An upload the account already
has (409) reuses the existing asset, and a new upload appears unselected in the media
list. One edit runs at a time. Failures that outlive the dialog show in the status
line as "Ad #N: text not saved: ...". A 401 returns to sign-in.

Known limits: the throttle is per login, so anyone who knows an account ID (they are
public) can keep its login failing with one bad attempt every 30 seconds; add per-IP
limits before using this beyond demos. At most 4 scrypt checks run at once per process.

## Objects and fields

An ad contains `id`, `system_name` (`ad_<id>`), `image_hash`, `text`,
`creator_id`, `creation_time` (Unix seconds), `update_time` (Unix seconds of the last edit, `None`
until the first), and `video_id`. IDs (accounts,
ads, videos) are integers from 0 to 2^63 − 1, given as ints or digit strings;
anything else is invalid (`002`) for writes and missing (`None`) for reads,
including any account created with a negative ID by an earlier build. Image ads
have `video_id: None`; video ads have `image_hash: None`. Errors contain
`error_code` and `error_message`: `001` means an unknown/duplicate field, `002`
an invalid value, and `003` a storage failure while handling the named field or
resource (`ad`, `account`, `image`, or `video`); the same request may succeed
when retried. The HTTP API adds `004` for a duplicate upload. Reads return `None` only for records that are missing. Text must
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
| `previews.py` | Ad preview formats, their warnings, and escaped static HTML pages. |
| `api.py` | FastAPI HTTP API over `AdsService`; maps errors to HTTP status codes; session cookie and web UI routes. |
| `web/` | The web UI: `index.html`, `app.js`, `app.css`. |

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
databases gain the `videos`, `image_owners`, `users`, and `sessions` tables, the
`ads.video_id` and `accounts.token_hash` columns, and an index for listing a creator's
ads;
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

Edits go through `AdsService.update_ad(ad_id, data)`. A custom repository must
implement `update_ad(draft, update_time)`. Duck-typed validators need
`validate_update(ad_id, data)` and custom publishers need `update(ad_id, data)`;
without them, edits fail with `003`. An override of `AdValidator.validate` does not
run on edits. Rules see the draft with `ad_id` set and `creation_time=None`.

## Storage

By default, generated data lives beside the modules under `data/`:

- `ads.db`: SQLite accounts, ads, videos, image ownership, web logins, and sessions
  (session tokens are stored only as SHA-256 hashes).
- `image_store_data/`: copied local files, named by the first 32 hex characters
  of their SHA-256 with no extension, and `persisted_images.json`. Registered
  paths are absolute: a copied data directory keeps its local copy on re-upload,
  but its registry entries still name the original location.
- `videos/`: uploaded video files, named the same way.

Uploads are deduplicated before they reach storage. The source is hashed first;
if a file with that hash is already stored, including an image that an older
build stored as `<hash><suffix>`, nothing is written. Otherwise the file is
copied and checked against the first hash; a source that changed in between
returns `002` for `file`. A file's name depends only on its content, so
byte-identical uploads never get two names, even when they arrive at the same
time. Similar but different content, such as the same picture in another aspect
ratio, has a different hash and is stored separately.

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

The HTTP API tests need the packages in `requirements.txt`, and the browser tests
also need Playwright and Chromium from `requirements-dev.txt`; each is skipped without
its packages. Run every suite from the virtual environment:

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_logins.py` covers web logins and sessions in the repository, service, and
`authenticate`; `tests/test_api_logins.py` covers their HTTP routes, the session cookie on
account routes, and the UI files; `tests/test_ui_browser.py` drives the page in headless
Chromium against a real server.

`tests/test_api.py` drives the app in process with FastAPI's `TestClient`;
`tests/test_api_e2e.py` starts a real uvicorn server on a free port and calls
it over HTTP, covering chunked uploads, configuration from the environment,
persistence across a server restart, and media access: owner-only by token or signed
URL, URLs valid across processes sharing a key, and revocation when another process
reissues the token. Ad previews are driven the way a browser would use them: list an
ad's previews with the token, open each page by its signed URL, check its headers
(including a `media-src` naming the real host), and fetch every media link it embeds,
across server processes and after a token reissued elsewhere. `tests/test_api.py` checks
share links: their 7-day lifetime, revocation, and the identical 404 page.

The tests use temporary storage and include fresh-process persistence checks.
They are adapted from the original task's behavior tests for the canonical API;
benchmark isolation, API discovery, and reward reporting are omitted.
Additional checks cover the object API, custom rules, custom publishing stages,
injected image storage and clocks, and compatibility with existing SQLite data.
The standard-library suite has 135 tests (212 with the HTTP API suites, 240 with the
browser tests), including boundary cases, storage failures, rollback,
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
