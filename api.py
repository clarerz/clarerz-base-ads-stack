"""Public HTTP API over AdsService. Run with: ADS_ADMIN_KEY=<secret> uvicorn api:app"""

import hmac
import os
from pathlib import Path
import re
import tempfile

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException

import config
import image_store
import publish
from repository import SQLiteAdsRepository
from service import AdsService
from validation import (IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AdValidator, ProjectionValidator, StorageError,
                        ValidationError, authenticate, coerce_id, existing_account_id,
                        validate_name)
import video_store

MAX_JSON_BYTES = 64 * 1024
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


def status_for(exc):
    status = getattr(exc, "status", None)
    if status is not None:
        return status
    if exc.code == "003":
        return 503
    if exc.code == "002" and exc.field == "access_token":
        return 401
    return 400


def call(resource, fn, *args, not_found=None):
    """Run a service call; unexpected failures become 003 for resource, like publish.py."""
    try:
        return fn(*args)
    except ValidationError as exc:
        # Some routes report a field's invalid value as a missing resource.
        if exc.code == "002" and exc.field == not_found:
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
    """Write an authenticated upload's body to a temp file; return (owner_id, path).

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
    try:
        with spool:
            size = 0
            async for chunk in request.stream():
                # The server enforces Content-Length; counting guards the limit regardless.
                size += len(chunk)
                if size > max_bytes:
                    raise with_status(ValidationError("002", "file"), 413)
                spool.write(chunk)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise StorageError("file") from exc
    except BaseException:
        # Includes a client that disconnects mid-body.
        path.unlink(missing_ok=True)
        raise
    return owner_id, path


@router.post("/accounts/{account_id}/images", status_code=201)
async def upload_image(account_id: str, request: Request, filename: str | None = None,
                       image_name: str | None = None, token=Depends(bearer_token)):
    data = {}
    if image_name is not None:
        data["image_name"] = validate_name(image_name, "image_name")
    owner_id, path = await spool_upload(request, account_id, token, filename, IMAGE_EXTENSIONS,
                                        image_store.MAX_IMAGE_BYTES)
    try:
        image_hash = await run_in_threadpool(
            call, "image", request.app.state.service.upload_image,
            {**data, "owner_id": owner_id, "access_token": token, "file": str(path)})
    finally:
        path.unlink(missing_ok=True)
    return {"hash": image_hash}


@router.get("/accounts/{account_id}/images")
def list_images(account_id: str, page=Depends(page_params), token=Depends(bearer_token),
                service=Depends(get_service)):
    images, next_cursor = call("image", service.list_images, account_id, token, *page)
    return {"data": [image.to_dict() for image in images], "next_cursor": next_cursor}


@router.post("/accounts/{account_id}/videos", status_code=201)
async def upload_video(account_id: str, request: Request, filename: str | None = None,
                       name: str | None = None, token=Depends(bearer_token)):
    name = validate_name(name)
    owner_id, path = await spool_upload(request, account_id, token, filename, VIDEO_EXTENSIONS,
                                        video_store.MAX_VIDEO_BYTES)
    try:
        video = await run_in_threadpool(
            call, "video", request.app.state.service.upload_video,
            {"owner_id": owner_id, "access_token": token, "name": name, "file": str(path)})
    finally:
        path.unlink(missing_ok=True)
    return {"id": video.id}


@router.get("/accounts/{account_id}/videos")
def list_videos(account_id: str, page=Depends(page_params), token=Depends(bearer_token),
                service=Depends(get_service)):
    videos, next_cursor = call("video", service.list_videos, account_id, token, *page)
    return {"data": [video.to_dict() for video in videos], "next_cursor": next_cursor}


# A missing video and another account's video get the same 404, so IDs cannot be probed.
@router.get("/videos/{video_id}")
def get_video(video_id: str, token=Depends(bearer_token), service=Depends(get_service)):
    return call("video", service.get_video, video_id, token, not_found="video_id").to_dict()


@router.patch("/videos/{video_id}")
def rename_video(video_id: str, body: dict = Body(...), token=Depends(bearer_token),
                 service=Depends(get_service)):
    data = check_fields(body, {"name"})
    return call("video", service.rename_video, video_id, data.get("name"), token,
                not_found="video_id").to_dict()


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


def create_app(service=None, admin_key=None, spool_dir=None):
    app = FastAPI(title="Ads publishing API")
    app.state.service = (service if service is not None
                         else AdsService(SQLiteAdsRepository(publish.DB_PATH)))
    app.state.admin_key = admin_key if admin_key is not None else os.environ.get("ADS_ADMIN_KEY")
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
