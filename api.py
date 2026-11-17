"""Public HTTP API over AdsService. Run with: ADS_ADMIN_KEY=<secret> uvicorn api:app"""

import hashlib
import hmac
import os
from pathlib import Path
import re
import tempfile

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException

import config
import file_store
import image_store
from models import Image
import previews
import publish
from repository import SQLiteAdsRepository
from service import SESSION_SECONDS, AdsService
from validation import (HASH_REGEX, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AdValidator,
                        ProjectionValidator, StorageError, ValidationError, authenticate,
                        coerce_id, existing_account_id, hash_token, validate_name)
import video_store

MAX_JSON_BYTES = 64 * 1024
# Preview URLs let <img> and <video> load stored bytes, which they cannot do with a bearer
# token; each URL is signed for one account's file and expires after this many seconds.
PREVIEW_SECONDS = 600
SHARE_SECONDS = 7 * 24 * 3600
MEDIA_DIRS = {"images": Path(image_store._PERSIST_DIR), "videos": video_store.VIDEO_DIR}
# Upload routes stream their bodies to disk under their own limits; other bodies are JSON.
_UPLOAD_PATH = re.compile(r"/accounts/[^/]+/(images|videos)")
router = APIRouter()
# The field named in errors the framework raises before a route runs.
HTTP_ERROR_FIELDS = {400: "body", 405: "method"}
_bearer = HTTPBearer(auto_error=False)
SESSION_COOKIE = "ads_session"
# The web UI sends this header on every call. Cross-origin pages cannot send it without a CORS
# preflight, which this server never approves, so it marks a request as same-origin.
UI_HEADER = "X-Ads-UI"


def with_status(exc, status):
    """Attach an HTTP status that the error code alone cannot express."""
    exc.status = status
    return exc


class DuplicateUpload(ValidationError):
    """The account already uploaded this content; the body names the existing asset."""

    def __init__(self, **existing):
        super().__init__("004", "file")
        self.existing = existing

    def to_dict(self):
        return {**super().to_dict(), **self.existing}


def status_for(exc):
    status = getattr(exc, "status", None)
    if status is not None:
        return status
    if exc.code == "003":
        return 503
    if exc.code == "004":
        return 409
    if exc.code == "002" and exc.field == "access_token":
        return 401
    return 400


def call(resource, fn, *args, not_found=(), unauthorized=()):
    """Run a service call; unexpected failures become 003 for resource, like publish.py."""
    try:
        return fn(*args)
    except ValidationError as exc:
        # Some routes report a field's invalid value as a missing resource or failed login.
        if exc.code == "002" and exc.field in not_found:
            raise with_status(exc, 404)
        if exc.code == "002" and exc.field in unauthorized:
            raise with_status(exc, 401)
        raise
    except Exception as exc:
        raise StorageError(resource) from exc


def found(value, field):
    if value is None:
        raise with_status(ValidationError("002", field), 404)
    return value


def check_fields(body, allowed):
    if not isinstance(body, dict):
        raise ValidationError("002", "body")
    for field in body:
        if field not in allowed:
            raise ValidationError("001", field)
    return body


def get_service(request: Request):
    return request.app.state.service


def header_token(credentials=Depends(_bearer)):
    # None for a missing header, another scheme, or an empty token; the checks reject None.
    return credentials.credentials if credentials is not None else None


def bearer_token(request: Request, token=Depends(header_token)):
    """The Authorization bearer token or, for a UI request with no Authorization header, the
    session cookie, which authenticate accepts for the session's own account."""
    if "authorization" in request.headers or request.headers.get(UI_HEADER) != "1":
        return token
    session = request.cookies.get(SESSION_COOKIE)
    request.state.session_auth = session is not None
    return session


def require_admin(request: Request, token=Depends(header_token)):
    # A dependency, so it runs before the body is validated. No configured key: no creates.
    key = request.app.state.admin_key
    if not (key and token and hmac.compare_digest(key.encode(), token.encode())):
        raise ValidationError("002", "access_token")


@router.post("/accounts", status_code=201, dependencies=[Depends(require_admin)])
def create_account(body: dict = Body(...), service=Depends(get_service)):
    data = check_fields(body, {"id", "name"})
    account, token = call("account", service.create_account, data.get("id"), data.get("name"))
    return {"id": account.id, "access_token": token}


@router.get("/accounts/{account_id}")
def get_account(account_id: str, service=Depends(get_service)):
    return found(call("account", service.get_account, account_id), "account_id").to_dict()


@router.patch("/accounts/{account_id}")
def rename_account(account_id: str, body: dict = Body(...), token=Depends(bearer_token),
                   service=Depends(get_service)):
    data = check_fields(body, {"name"})
    return call("account", service.rename_account, account_id, data.get("name"),
                token).to_dict()


def require_ui(request, field):
    if request.headers.get(UI_HEADER) != "1":
        raise with_status(ValidationError("002", field), 401)


def session_json(user, account):
    return {"username": user.username, "account_id": account.id, "account_name": account.name}


@router.post("/accounts/{account_id}/users", status_code=201)
def create_user(account_id: str, request: Request, body: dict = Body(...),
                token=Depends(header_token), service=Depends(get_service)):
    """Give the account its web login; the access token proves ownership once, so the
    session cookie never counts here."""
    require_ui(request, "access_token")
    data = check_fields(body, {"username", "password"})
    user = call("account", service.create_user, account_id, token, data.get("username"),
                data.get("password"))
    return {"username": user.username, "account_id": user.account_id}


@router.post("/session")
def login(request: Request, response: Response, body: dict = Body(...),
          service=Depends(get_service)):
    require_ui(request, "login")
    data = check_fields(body, {"login", "password"})
    # A login replaces the browser's previous session, so a planted cookie never lives on.
    token, user, account = call("login", service.login, data.get("login"), data.get("password"),
                                request.cookies.get(SESSION_COOKIE), unauthorized=("login",))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, path="/",
                        secure=request.app.state.secure_cookies, httponly=True,
                        samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return session_json(user, account)


@router.get("/session")
def get_session(request: Request, response: Response, service=Depends(get_service)):
    session = call("login", service.get_session, request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise with_status(ValidationError("002", "login"), 401)
    response.headers["Cache-Control"] = "no-store"
    return session_json(*session)


@router.delete("/session", status_code=204)
def logout(request: Request, service=Depends(get_service)):
    require_ui(request, "login")
    call("login", service.logout, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=request.app.state.secure_cookies,
                           httponly=True, samesite="strict")
    return response


def page_params(limit: str | None = None, cursor: str | None = None, since: str | None = None,
                until: str | None = None):
    """Listing query parameters, passed to the service as strings for it to validate."""
    return limit, cursor, since, until


@router.post("/accounts/{account_id}/ads", status_code=201)
def publish_ad(account_id: str, body: dict = Body(...), token=Depends(bearer_token),
               service=Depends(get_service)):
    # The creator comes from the path and the token from the header, never the body.
    data = check_fields(body, AdValidator.allowed_fields - {"creator_id", "access_token"})
    ad = call("ad", service.publish_ad, {**data, "creator_id": account_id, "access_token": token})
    return {"id": ad.id}


@router.get("/accounts/{account_id}/ads")
def list_ads(account_id: str, page=Depends(page_params), service=Depends(get_service)):
    ads, next_cursor = call("ad", service.list_ads, account_id, *page)
    return {"data": [ad.to_dict() for ad in ads], "next_cursor": next_cursor}


@router.get("/ads/{ad_id}")
def get_ad(ad_id: str, fields: str | None = None, service=Depends(get_service)):
    ad = found(call("ad", service.get_ad, ad_id), "ad_id")
    return ProjectionValidator().project(ad, fields)


def _authenticated_owner(repository, account_id, token, field="owner_id"):
    owner_id = existing_account_id(repository, account_id, field)
    authenticate(repository, owner_id, token)
    return owner_id


async def spool_upload(request, account_id, token, filename, extensions, max_bytes):
    """Write an authenticated upload's body to a temp file; return (owner_id, path, hash).

    Every check runs before anything is written, so rejected uploads cost no disk. Only
    the suffix of filename is used: clients never choose a server path. The caller
    deletes the returned path.
    """
    state = request.app.state
    owner_id = await run_in_threadpool(_authenticated_owner, state.service.repository,
                                       account_id, token)
    suffix = Path(filename or "").suffix
    if suffix.lower() not in extensions:
        raise ValidationError("002", "file")
    length = request.headers.get("content-length")
    if length is None:
        raise with_status(ValidationError("002", "file"), 411)
    length = coerce_id(length)
    if length is None:
        raise ValidationError("002", "file")
    if length > max_bytes:
        raise with_status(ValidationError("002", "file"), 413)
    try:
        state.spool_dir.mkdir(parents=True, exist_ok=True)
        spool = tempfile.NamedTemporaryFile(dir=state.spool_dir, suffix=suffix, delete=False)
    except OSError as exc:  # Disk full or unwritable data directory.
        raise StorageError("file") from exc
    path = Path(spool.name)
    # Hashed while spooling, with the 32-character content hash file_store names files by.
    digest = hashlib.sha256()
    try:
        with spool:
            size = 0
            async for chunk in request.stream():
                # The server enforces Content-Length; counting guards the limit regardless.
                size += len(chunk)
                if size > max_bytes:
                    raise with_status(ValidationError("002", "file"), 413)
                digest.update(chunk)
                spool.write(chunk)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise StorageError("file") from exc
    except BaseException:
        # Includes a client that disconnects mid-body.
        path.unlink(missing_ok=True)
        raise
    return owner_id, path, digest.hexdigest()[:32]


@router.post("/accounts/{account_id}/images", status_code=201)
async def upload_image(account_id: str, request: Request, filename: str | None = None,
                       image_name: str | None = None, token=Depends(bearer_token)):
    data = {}
    if image_name is not None:
        data["image_name"] = validate_name(image_name, "image_name")
    owner_id, path, content_hash = await spool_upload(request, account_id, token, filename,
                                                      IMAGE_EXTENSIONS, image_store.MAX_IMAGE_BYTES)
    service = request.app.state.service
    try:
        # Only this account's own uploads count, so the error never reveals another
        # account's files; checked first, so a duplicate changes nothing (not even the name).
        if await run_in_threadpool(call, "file", service.repository.owns_image, owner_id,
                                   content_hash):
            raise DuplicateUpload(hash=content_hash)
        image_hash = await run_in_threadpool(
            call, "image", service.upload_image,
            {**data, "owner_id": owner_id, "access_token": token, "file": str(path)})
    finally:
        path.unlink(missing_ok=True)
    return {"hash": image_hash}


@router.get("/accounts/{account_id}/images")
def list_images(account_id: str, request: Request, page=Depends(page_params),
                token=Depends(bearer_token), service=Depends(get_service)):
    images, next_cursor = call("image", service.list_images, account_id, token, *page)
    token_hash = signing_hash(request, coerce_id(account_id), token)
    return {"data": [image_json(request, token_hash, image) for image in images],
            "next_cursor": next_cursor}


# A missing image and another account's image get the same 404, so hashes cannot be probed.
@router.get("/accounts/{account_id}/images/lookup")
def get_image(account_id: str, request: Request, image_hash: str | None = None,
              image_name: str | None = None, token=Depends(bearer_token),
              service=Depends(get_service)):
    image = call("image", service.get_image, account_id, token, image_hash, image_name,
                 not_found=("image_hash", "image_name"))
    return image_json(request, signing_hash(request, coerce_id(account_id), token), image)


@router.post("/accounts/{account_id}/videos", status_code=201)
async def upload_video(account_id: str, request: Request, filename: str | None = None,
                       name: str | None = None, token=Depends(bearer_token)):
    name = validate_name(name)
    owner_id, path, content_hash = await spool_upload(request, account_id, token, filename,
                                                      VIDEO_EXTENSIONS, video_store.MAX_VIDEO_BYTES)
    service = request.app.state.service
    try:
        # ponytail: check-then-insert, so two simultaneous identical uploads can both succeed;
        # add a unique (owner_id, video_hash) index if that must never happen.
        existing = await run_in_threadpool(call, "file", service.repository.get_video_by_hash,
                                           owner_id, content_hash)
        if existing is not None:
            raise DuplicateUpload(id=existing.id)
        video = await run_in_threadpool(
            call, "video", service.upload_video,
            {"owner_id": owner_id, "access_token": token, "name": name, "file": str(path)})
    finally:
        path.unlink(missing_ok=True)
    return {"id": video.id}


@router.get("/accounts/{account_id}/videos")
def list_videos(account_id: str, request: Request, page=Depends(page_params),
                token=Depends(bearer_token), service=Depends(get_service)):
    videos, next_cursor = call("video", service.list_videos, account_id, token, *page)
    token_hash = signing_hash(request, coerce_id(account_id), token)
    return {"data": [video_json(request, token_hash, video) for video in videos],
            "next_cursor": next_cursor}


# A missing video and another account's video get the same 404, so IDs cannot be probed.
@router.get("/videos/{video_id}")
def get_video(video_id: str, request: Request, token=Depends(bearer_token),
              service=Depends(get_service)):
    video = call("video", service.get_video, video_id, token, not_found=("video_id",))
    return video_json(request, signing_hash(request, video.owner_id, token), video)


@router.patch("/videos/{video_id}")
def rename_video(video_id: str, request: Request, body: dict = Body(...),
                 token=Depends(bearer_token), service=Depends(get_service)):
    data = check_fields(body, {"name"})
    video = call("video", service.rename_video, video_id, data.get("name"), token,
                 not_found=("video_id",))
    return video_json(request, signing_hash(request, video.owner_id, token), video)


def media_signature(key, account_id, kind, media_hash, expires, token_hash):
    # Signing the account's token hash means issuing it a new token revokes its old URLs.
    message = f"{account_id}/{kind}/{media_hash}/{expires}/{token_hash}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def signed_url(request, token_hash, owner_id, scope, subject, route, expires=None, **path):
    """Return a URL for route signed for owner_id's scope and subject, valid until expires
    (default: PREVIEW_SECONDS from now).

    None without a key. Only callers that have authenticated owner_id (by token, session, or
    signed URL) call this; token_hash is the account's current one (see signing_hash).
    """
    key = request.app.state.media_key
    if not key:
        return None
    if expires is None:
        expires = int(request.app.state.service.clock()) + PREVIEW_SECONDS
    url = request.url_for(route, account_id=owner_id, **path)
    return str(url.include_query_params(exp=expires, sig=media_signature(
        key, owner_id, scope, subject, expires, token_hash)))


def media_url(request, token_hash, kind, owner_id, media_hash, expires=None):
    return signed_url(request, token_hash, owner_id, kind, media_hash, "get_media", expires,
                      kind=kind, media_hash=media_hash)


def signing_hash(request, owner_id, token):
    """Return the token hash to sign owner_id's URLs with, for a request already authenticated.

    A session's token is a session ID, not the account's token, so its URLs are signed with the
    account's stored hash; that is what signed_access checks them against.
    """
    if getattr(request.state, "session_auth", False):
        return call("account", request.app.state.service.repository.get_token_hash, owner_id)
    return hash_token(token)


def signed_access(request, owner_id, token, exp, sig, scope, subject, field):
    """Return (expiry, token hash) for a request with owner_id's token or a live URL signed for
    scope and subject, or None for any bad signature. A wrong token raises 401."""
    state = request.app.state
    repository = state.service.repository
    now = int(state.service.clock())
    if token is not None:
        # Like every account route, a wrong token is a 401 before anything is looked up.
        authenticate(repository, owner_id, token)
        return now + PREVIEW_SECONDS, signing_hash(request, owner_id, token)
    expires = coerce_id(exp)
    token_hash = (call(field, repository.get_token_hash, owner_id)
                  if owner_id is not None else None)
    valid = (state.media_key and isinstance(token_hash, str) and expires is not None
             and now < expires and sig is not None and hmac.compare_digest(
                 sig.encode(), media_signature(state.media_key, owner_id, scope, subject,
                                               expires, token_hash).encode()))
    return (expires, token_hash) if valid else None


def image_json(request, token_hash, image):
    # Link-ingested images have no stored bytes; their source link is the preview.
    return {**image.to_dict(), "preview_url": image.image_url or media_url(
        request, token_hash, "images", image.owner_id, image.hash)}


def video_json(request, token_hash, video):
    return {**video.to_dict(), "preview_url": media_url(
        request, token_hash, "videos", video.owner_id, video.video_hash)}


def owns_media(repository, owner_id, kind, media_hash):
    if kind == "images":
        return repository.owns_image(owner_id, media_hash)
    return repository.get_video_by_hash(owner_id, media_hash) is not None


def media_type(path):
    """Return the media type declared by a stored file's leading bytes."""
    # Files are stored without extensions. Unrecognized bytes are an opaque download, never
    # something a browser would render as a page.
    with open(path, "rb") as f:
        head = f.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] in (b"WEBP", b"AVI "):
        return "image/webp" if head[8:12] == b"WEBP" else "video/x-msvideo"
    if head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:12] == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):  # EBML: WebM declares its doctype early.
        return "video/webm" if b"webm" in head else "video/x-matroska"
    return "application/octet-stream"


@router.get("/accounts/{account_id}/media/{kind}/{media_hash}", name="get_media")
def get_media(account_id: str, kind: str, media_hash: str, request: Request,
              exp: str | None = None, sig: str | None = None, token=Depends(bearer_token)):
    """Serve an account's stored bytes for its token, or for a preview URL issued to it."""
    service = request.app.state.service
    repository = service.repository
    owner_id = coerce_id(account_id)
    # Every rejection gets the same 404, so a URL reveals nothing about why it failed.
    access = signed_access(request, owner_id, token, exp, sig, kind, media_hash, "media")
    # Ownership is checked on every request, not just when the URL was issued.
    owned = (access is not None and kind in MEDIA_DIRS and HASH_REGEX.fullmatch(media_hash)
             and call("media", owns_media, repository, owner_id, kind, media_hash))
    path = file_store.find_file(MEDIA_DIRS[kind], media_hash) if owned else None
    if path is None:
        raise with_status(ValidationError("002", "media"), 404)
    try:
        content_type = media_type(path)
    except OSError as exc:
        raise StorageError("media") from exc
    # The bytes behind a hash never change, so they can be cached until the URL expires
    # (for one URL lifetime when fetched with the token). A share link's media is cached for
    # at most PREVIEW_SECONDS, so revoking the account's token takes effect soon after.
    now = int(service.clock())
    return FileResponse(path, media_type=content_type, headers={
        "Cache-Control": f"private, max-age={min(access[0] - now, PREVIEW_SECONDS)}, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    })


def _account_ad(service, owner_id, ad_id):
    """Return the account's ad, or None; another account's ad is as missing as no ad."""
    ad = service.get_ad(ad_id)
    return ad if ad is not None and ad.creator_id == owner_id else None


def _ad_image(service, ad):
    """Return (image described with its link and size, whether the creator owns it)."""
    image = service.repository.get_image(ad.creator_id, ad.image_hash)
    # Ads published before image ownership was recorded still preview their link image.
    owned = image is not None
    image = image or Image(ad.image_hash, ad.creator_id, ad.creation_time)
    return service.describe_image(image), owned


def _ad_size(service, ad):
    """Return the (width, height) of the ad's image, or (None, None) for a video ad."""
    if ad.image_hash is None:
        return None, None
    image = _ad_image(service, ad)[0]
    return image.width, image.height


def _stored_media_src(request, access, kind, owner_id, media_hash):
    """Return a signed URL for stored bytes a browser can show as kind, else None."""
    path = file_store.find_file(MEDIA_DIRS[kind], media_hash)
    # Bytes that are not media of that kind (like the sample.jpg placeholder) would show
    # as a broken element; the page shows a labeled empty box instead.
    prefix = {"images": "image/", "videos": "video/"}[kind]
    if path is None or not media_type(path).startswith(prefix):
        return None
    expires, token_hash = access
    return media_url(request, token_hash, kind, owner_id, media_hash, expires)


def preview_page(request, owner_id, ad_id, ad_format, access):
    """Return the HTML preview of the account's ad in ad_format, or None if there is none.

    access is the (expiry, token hash) the request was granted; media links in the page are
    signed with both, so they never outlive the preview URL or the account's token.
    """
    service = request.app.state.service
    ad = _account_ad(service, owner_id, ad_id)
    if ad is None or ad_format not in previews.FORMATS:
        return None
    account = service.repository.get_account(owner_id)
    if ad.video_id is not None:
        video = service.repository.get_video(ad.video_id)
        kind, src = "video", video and _stored_media_src(request, access, "videos",
                                                         owner_id, video.video_hash)
    else:
        image, owned = _ad_image(service, ad)
        kind, src = "image", image.image_url or (owned and _stored_media_src(
            request, access, "images", owner_id, image.hash))
    return previews.render(ad_format, account.name, ad.text, kind, src or None)


@router.get("/accounts/{account_id}/ads/{ad_id}/previews")
def list_ad_previews(account_id: str, ad_id: str, request: Request,
                     ad_format: str | None = None, token=Depends(bearer_token),
                     service=Depends(get_service)):
    """Signed preview page URLs for the account's ad, one per format, for <iframe> embedding."""
    owner_id = call("ad", _authenticated_owner, service.repository, account_id, token,
                    "creator_id")
    if ad_format is not None and ad_format not in previews.FORMATS:
        raise ValidationError("002", "ad_format")
    ad = found(call("ad", _account_ad, service, owner_id, ad_id), "ad_id")
    width, height = call("image", _ad_size, service, ad)
    token_hash = signing_hash(request, owner_id, token)
    share_expires = int(service.clock()) + SHARE_SECONDS
    share_url = signed_url(request, token_hash, owner_id, "share", ad.id, "get_ad_share",
                           expires=share_expires, ad_id=ad.id)
    return {"data": [
        {"format": name,
         "preview_url": signed_url(request, token_hash, owner_id, f"preview:{name}", ad.id,
                                   "get_ad_preview", ad_id=ad.id, ad_format=name),
         "width": spec["width"], "height": spec["height"],
         "warnings": previews.warnings(name, ad.text, width, height)}
        for name, spec in previews.FORMATS.items() if ad_format in (None, name)],
        "share_url": share_url, "share_expires": share_expires if share_url else None}


def share_page(request, owner_id, ad_id, access):
    """Return the HTML page framing the account's ad in every format, or None if there is none.

    access is the (expiry, token hash) the request was granted; every frame URL carries both,
    so nothing on the page outlives the link or the account's token.
    """
    service = request.app.state.service
    ad = _account_ad(service, owner_id, ad_id)
    # Without a media key there are no signed frame URLs to put on the page.
    if ad is None or not request.app.state.media_key:
        return None
    expires, token_hash = access
    width, height = _ad_size(service, ad)
    entries = [{"format": name,
                "url": signed_url(request, token_hash, owner_id, f"preview:{name}", ad.id,
                                  "get_ad_preview", expires=expires, ad_id=ad.id,
                                  ad_format=name),
                "width": spec["width"], "height": spec["height"],
                "warnings": previews.warnings(name, ad.text, width, height)}
               for name, spec in previews.FORMATS.items()]
    account = service.repository.get_account(owner_id)
    return previews.render_share(account.name, ad.id, expires, entries)


@router.get("/accounts/{account_id}/ads/{ad_id}/share", name="get_ad_share")
def get_ad_share(account_id: str, ad_id: str, request: Request, exp: str | None = None,
                 sig: str | None = None, token=Depends(bearer_token)):
    """Serve the share page for the account's token, or for a share URL issued to it."""
    owner_id = coerce_id(account_id)
    access = signed_access(request, owner_id, token, exp, sig, "share", ad_id, "share")
    page = access is not None and call("share", share_page, request, owner_id, ad_id, access)
    origin = f"{request.url.scheme}://{request.url.netloc}"
    headers = {
        # No scripts run, the page gets an opaque origin, and it frames only this server's
        # preview pages (signed URLs).
        "Content-Security-Policy": f"sandbox; default-src 'none'; frame-src {origin}; "
                                   "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff",
        # The page URL carries its signature.
        "Referrer-Policy": "no-referrer",
        # Frame URLs expire, so a cached page would go stale.
        "Cache-Control": "no-store",
    }
    # Every rejection gets the same page, so a link reveals nothing about why it failed.
    if not page:
        return HTMLResponse(previews.SHARE_NOT_FOUND, 404, headers=headers)
    return HTMLResponse(page, headers=headers)


@router.get("/accounts/{account_id}/ads/{ad_id}/previews/{ad_format}", name="get_ad_preview")
def get_ad_preview(account_id: str, ad_id: str, ad_format: str, request: Request,
                   exp: str | None = None, sig: str | None = None, token=Depends(bearer_token)):
    """Serve the preview page for the account's token, or for a preview URL issued to it."""
    owner_id = coerce_id(account_id)
    # The scope names the format, so a URL opens one ad in one format and is never a media URL.
    access = signed_access(request, owner_id, token, exp, sig, f"preview:{ad_format}", ad_id,
                           "preview")
    page = access is not None and call("preview", preview_page, request, owner_id, ad_id,
                                       ad_format, access)
    if not page:
        raise with_status(ValidationError("002", "preview"), 404)
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return HTMLResponse(page, headers={
        # No scripts run, the page gets an opaque origin, and media loads only from this
        # server (signed URLs) or, for link images, the advertiser's link.
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src http: https:; "
                                   f"media-src {origin}; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
        # The page URL carries its signature; never send it to a link image's host.
        "Referrer-Policy": "no-referrer",
        # Embedded media URLs expire, so a cached page would go stale.
        "Cache-Control": "no-store",
    })


WEB_DIR = Path(__file__).resolve().parent / "web"
UI_ASSETS = {"app.js": "text/javascript", "app.css": "text/css"}
UI_HEADERS = {
    # Scripts and styles come only from the files below; API calls only to this server.
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; img-src 'self'; frame-src 'self'; "
                               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# Explicit routes rather than a StaticFiles mount, which would redirect /ui to /ui/.
@router.get("/ui", include_in_schema=False)
def ui_page():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=UI_HEADERS)


@router.get("/ui/{asset}", include_in_schema=False)
def ui_asset(asset: str):
    if asset not in UI_ASSETS:
        raise with_status(ValidationError("002", "path"), 404)
    return FileResponse(WEB_DIR / asset, media_type=UI_ASSETS[asset], headers=UI_HEADERS)


class JsonBodyLimit:
    """Refuse oversized or unsized JSON bodies before FastAPI reads them into memory.

    FastAPI reads and parses a body before any dependency runs, so without this an
    unauthenticated client could make the server buffer a body of any size.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not (scope["method"] == "POST"
                                            and _UPLOAD_PATH.fullmatch(scope["path"])):
            headers = dict(scope["headers"])
            length = headers.get(b"content-length")
            status = None
            if length is None:
                # A chunked body's size is unknown until it has been read.
                if b"transfer-encoding" in headers:
                    status = 411
            elif (coerce_id(length.decode("latin-1")) or 0) > MAX_JSON_BYTES:
                status = 413
            if status is not None:
                response = JSONResponse(ValidationError("002", "body").to_dict(), status)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(service=None, admin_key=None, spool_dir=None, media_key=None,
               secure_cookies=None):
    app = FastAPI(title="Ads publishing API")
    app.state.service = (service if service is not None
                         else AdsService(SQLiteAdsRepository(publish.DB_PATH)))
    app.state.admin_key = admin_key if admin_key is not None else os.environ.get("ADS_ADMIN_KEY")
    # No key: previews carry no URL and every media request is a 404.
    app.state.media_key = media_key if media_key is not None else os.environ.get("ADS_MEDIA_KEY")
    # Session cookies are Secure unless plain-HTTP development opts out explicitly; browsers
    # accept Secure cookies from http://localhost.
    app.state.secure_cookies = (secure_cookies if secure_cookies is not None
                                else os.environ.get("ADS_INSECURE_COOKIES") != "1")
    app.state.spool_dir = Path(spool_dir) if spool_dir is not None else config.DATA_DIR / "uploads"
    app.add_exception_handler(
        ValidationError, lambda request, exc: JSONResponse(exc.to_dict(), status_for(exc)))
    # One error shape: malformed bodies get 002 for body, not FastAPI's 422 detail.
    app.add_exception_handler(
        RequestValidationError,
        lambda request, exc: JSONResponse(ValidationError("002", "body").to_dict(), 400))
    # Framework errors (unparsable bodies, unknown paths, wrong methods) use the same shape.
    app.add_exception_handler(
        HTTPException,
        lambda request, exc: JSONResponse(
            ValidationError("002", HTTP_ERROR_FIELDS.get(exc.status_code, "path")).to_dict(),
            exc.status_code, headers=exc.headers))
    app.add_middleware(JsonBodyLimit)
    app.include_router(router)
    return app


app = create_app()
