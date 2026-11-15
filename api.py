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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException

import config
import file_store
import image_store
import publish
from repository import SQLiteAdsRepository
from service import AdsService
from validation import (HASH_REGEX, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AdValidator,
                        ProjectionValidator, StorageError, ValidationError, authenticate,
                        coerce_id, existing_account_id, hash_token, validate_name)
import video_store

MAX_JSON_BYTES = 64 * 1024
# Preview URLs let <img> and <video> load stored bytes, which they cannot do with a bearer
# token; each URL is signed for one account's file and expires after this many seconds.
PREVIEW_SECONDS = 600
MEDIA_DIRS = {"images": Path(image_store._PERSIST_DIR), "videos": video_store.VIDEO_DIR}
# Upload routes stream their bodies to disk under their own limits; other bodies are JSON.
_UPLOAD_PATH = re.compile(r"/accounts/[^/]+/(images|videos)")
router = APIRouter()
# The field named in errors the framework raises before a route runs.
HTTP_ERROR_FIELDS = {400: "body", 405: "method"}
_bearer = HTTPBearer(auto_error=False)


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


def call(resource, fn, *args, not_found=()):
    """Run a service call; unexpected failures become 003 for resource, like publish.py."""
    try:
        return fn(*args)
    except ValidationError as exc:
        # Some routes report a field's invalid value as a missing resource.
        if exc.code == "002" and exc.field in not_found:
            raise with_status(exc, 404)
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


def bearer_token(credentials=Depends(_bearer)):
    # None for a missing header, another scheme, or an empty token; the checks reject None.
    return credentials.credentials if credentials is not None else None


def require_admin(request: Request, token=Depends(bearer_token)):
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


def _authenticated_owner(repository, account_id, token):
    owner_id = existing_account_id(repository, account_id, "owner_id")
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
    return {"data": [image_json(request, token, image) for image in images],
            "next_cursor": next_cursor}


# A missing image and another account's image get the same 404, so hashes cannot be probed.
@router.get("/accounts/{account_id}/images/lookup")
def get_image(account_id: str, request: Request, image_hash: str | None = None,
              image_name: str | None = None, token=Depends(bearer_token),
              service=Depends(get_service)):
    image = call("image", service.get_image, account_id, token, image_hash, image_name,
                 not_found=("image_hash", "image_name"))
    return image_json(request, token, image)


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
    return {"data": [video_json(request, token, video) for video in videos],
            "next_cursor": next_cursor}


# A missing video and another account's video get the same 404, so IDs cannot be probed.
@router.get("/videos/{video_id}")
def get_video(video_id: str, request: Request, token=Depends(bearer_token),
              service=Depends(get_service)):
    return video_json(request, token, call("video", service.get_video, video_id, token,
                                           not_found=("video_id",)))


@router.patch("/videos/{video_id}")
def rename_video(video_id: str, request: Request, body: dict = Body(...),
                 token=Depends(bearer_token), service=Depends(get_service)):
    data = check_fields(body, {"name"})
    return video_json(request, token, call("video", service.rename_video, video_id,
                                           data.get("name"), token, not_found=("video_id",)))


def media_signature(key, account_id, kind, media_hash, expires, token_hash):
    # Signing the account's token hash means issuing it a new token revokes its old URLs.
    message = f"{account_id}/{kind}/{media_hash}/{expires}/{token_hash}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def media_url(request, token, kind, owner_id, media_hash):
    """Return the owner's URL for stored bytes, valid for PREVIEW_SECONDS, or None without a key.

    Only routes that have already authenticated token as owner_id's call this.
    """
    key = request.app.state.media_key
    if not key:
        return None
    expires = int(request.app.state.service.clock()) + PREVIEW_SECONDS
    url = request.url_for("get_media", account_id=owner_id, kind=kind, media_hash=media_hash)
    return str(url.include_query_params(exp=expires, sig=media_signature(
        key, owner_id, kind, media_hash, expires, hash_token(token))))


def image_json(request, token, image):
    # Link-ingested images have no stored bytes; their source link is the preview.
    return {**image.to_dict(), "preview_url": image.image_url or media_url(
        request, token, "images", image.owner_id, image.hash)}


def video_json(request, token, video):
    return {**video.to_dict(),
            "preview_url": media_url(request, token, "videos", video.owner_id, video.video_hash)}


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
    state = request.app.state
    repository = state.service.repository
    owner_id = coerce_id(account_id)
    now = int(state.service.clock())
    if token is not None:
        # Like every account route, a wrong token is a 401 before anything is looked up.
        authenticate(repository, owner_id, token)
        expires = now + PREVIEW_SECONDS
        allowed = True
    else:
        # Every rejection gets the same 404, so a URL reveals nothing about why it failed.
        expires = coerce_id(exp)
        token_hash = (call("media", repository.get_token_hash, owner_id)
                      if owner_id is not None else None)
        allowed = (state.media_key and isinstance(token_hash, str) and expires is not None
                   and now < expires and sig is not None and hmac.compare_digest(
                       sig.encode(), media_signature(state.media_key, owner_id, kind, media_hash,
                                                     expires, token_hash).encode()))
    # Ownership is checked on every request, not just when the URL was issued.
    owned = (allowed and kind in MEDIA_DIRS and HASH_REGEX.fullmatch(media_hash)
             and call("media", owns_media, repository, owner_id, kind, media_hash))
    path = file_store.find_file(MEDIA_DIRS[kind], media_hash) if owned else None
    if path is None:
        raise with_status(ValidationError("002", "media"), 404)
    try:
        content_type = media_type(path)
    except OSError as exc:
        raise StorageError("media") from exc
    # The bytes behind a hash never change, so they can be cached until the URL expires
    # (for one URL lifetime when fetched with the token).
    return FileResponse(path, media_type=content_type, headers={
        "Cache-Control": f"private, max-age={expires - now}, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    })


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


def create_app(service=None, admin_key=None, spool_dir=None, media_key=None):
    app = FastAPI(title="Ads publishing API")
    app.state.service = (service if service is not None
                         else AdsService(SQLiteAdsRepository(publish.DB_PATH)))
    app.state.admin_key = admin_key if admin_key is not None else os.environ.get("ADS_ADMIN_KEY")
    # No key: previews carry no URL and every media request is a 404.
    app.state.media_key = media_key if media_key is not None else os.environ.get("ADS_MEDIA_KEY")
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
