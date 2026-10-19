"""HTTP layer (Starlette). Thin on purpose: authenticate, parse, call ``Archive``, map errors.

Every error leaves as ``{"type": code, "title": ..., "status": n, "detail": ...}`` (the shape of RFC 9457
problem details). Unexpected exceptions are logged on the server and answered with a generic 500.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .errors import ArchiveError, Invalid, TooLarge, Unauthorized
from .models import CorrectRequest, IngestRequest, RedactRequest
from .service import Archive, Principal

log = logging.getLogger("event_archive")
MAX_JSON = 64 * 1024
SECURITY_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


def problem(status: int, code: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"type": code, "title": code.replace("_", " "), "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def ok(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def _archive(request: Request) -> Archive:
    return request.app.state.archive


async def _principal(request: Request) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("send 'Authorization: Bearer <token>'")
    return await run_in_threadpool(_archive(request).authenticate, token.strip())


async def _read_body(request: Request, limit: int) -> bytes:
    """Read at most ``limit`` bytes; refuse early on a declared length and again while streaming
    (a chunked upload declares nothing)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise TooLarge(f"request body is larger than {limit} bytes")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise TooLarge(f"request body is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _model(request: Request, cls: type[BaseModel]):
    raw = await _read_body(request, MAX_JSON)
    try:
        data = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        raise Invalid("the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise Invalid("the request body must be a JSON object")
    return cls.model_validate(data)


def _int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not raw.lstrip("-").isdigit():
        raise Invalid(f"query parameter {name!r} must be an integer")
    return int(raw)


def _opt_bool(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    if raw.lower() not in ("true", "false", "1", "0"):
        raise Invalid(f"query parameter {name!r} must be true or false")
    return raw.lower() in ("true", "1")


# ----------------------------------------------------------------------------- endpoints
async def health(request: Request) -> Response:
    return ok({"status": "ok", "version": __version__})


async def me(request: Request) -> Response:
    p = await _principal(request)
    return ok({"username": p.username, "display_name": p.display_name, "role": p.role})


async def ingest(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, IngestRequest)
    event, created = await run_in_threadpool(_archive(request).ingest, p, body)
    headers = {"location": f"/events/{event['id']}"}
    if created:
        return ok(event, 201, headers)
    return ok(event, 200, {**headers, "idempotent-replayed": "true"})


async def get_event(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).get, p, request.path_params["event_id"]))


async def search(request: Request) -> Response:
    p = await _principal(request)
    qp = request.query_params
    result = await run_in_threadpool(
        lambda: _archive(request).search(
            p, qp.get("q"), asset_id=qp.get("asset_id"), kind=qp.get("kind"), severity=qp.get("severity"),
            since=qp.get("since"), until=qp.get("until"), has_recording=_opt_bool(request, "has_recording"),
            scope=qp.get("in", "all"), sort=qp.get("sort", "time"), limit=_int(request, "limit", 20),
            cursor=qp.get("cursor"), offset=_int(request, "offset", 0)))  # fmt: skip
    return ok(result)


async def put_recording(request: Request) -> Response:
    p = await _principal(request)
    a = _archive(request)
    audio = await _read_body(request, a.max_audio)
    event, created = await run_in_threadpool(a.attach_recording, p, request.path_params["event_id"], audio,
                                             request.headers.get("content-type"))  # fmt: skip
    return ok(event, 201 if created else 200, None if created else {"idempotent-replayed": "true"})


async def get_transcript(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).get_transcript, p, request.path_params["event_id"]))


async def correct_transcript(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, CorrectRequest)
    return ok(await run_in_threadpool(_archive(request).correct_transcript, p, request.path_params["event_id"], body))


async def retry_transcription(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).retry, p, request.path_params["event_id"]))


async def redact(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, RedactRequest)
    return ok(await run_in_threadpool(_archive(request).redact, p, request.path_params["event_id"], body))


async def run_worker(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).run_worker, p, 10))


async def audit_entries(request: Request) -> Response:
    p = await _principal(request)
    qp = request.query_params
    return ok(await run_in_threadpool(
        lambda: _archive(request).audit_entries(p, action=qp.get("action"), actor=qp.get("actor"),
                                                after_id=_int(request, "after_id", 0), limit=_int(request, "limit", 50))))  # fmt: skip


async def audit_verify(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).audit_verify, p, request.query_params.get("expected_head")))


async def stats(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_archive(request).stats, p))


# ----------------------------------------------------------------------------- errors
async def on_archive_error(request: Request, exc: ArchiveError) -> Response:
    headers = {"www-authenticate": 'Bearer realm="event-archive"'} if isinstance(exc, Unauthorized) else None
    return problem(exc.status, exc.code, exc.detail, headers, **exc.extra)


async def on_validation_error(request: Request, exc: ValidationError) -> Response:
    errors = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return problem(422, "validation_error", "the request body is not valid", errors=errors)


async def on_http_exception(request: Request, exc: HTTPException) -> Response:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return problem(exc.status_code, code, "no such route" if exc.status_code == 404 else str(exc.detail),
                   dict(exc.headers or {}))  # fmt: skip


async def on_unexpected(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return problem(500, "internal_error", "something went wrong on the server")


def create_app(archive: Archive) -> Starlette:
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/me", me, methods=["GET"]),
            Route("/stats", stats, methods=["GET"]),
            Route("/events", ingest, methods=["POST"]),
            Route("/events/search", search, methods=["GET"]),
            Route("/events/{event_id:int}", get_event, methods=["GET"]),
            Route("/events/{event_id:int}/recording", put_recording, methods=["PUT"]),
            Route("/events/{event_id:int}/transcript", get_transcript, methods=["GET"]),
            Route("/events/{event_id:int}/transcript/correct", correct_transcript, methods=["POST"]),
            Route("/events/{event_id:int}/transcript/retry", retry_transcription, methods=["POST"]),
            Route("/events/{event_id:int}/redact", redact, methods=["POST"]),
            Route("/worker/run", run_worker, methods=["POST"]),
            Route("/audit", audit_entries, methods=["GET"]),
            Route("/audit/verify", audit_verify, methods=["GET"]),
        ],
        exception_handlers={
            ArchiveError: on_archive_error,
            ValidationError: on_validation_error,
            HTTPException: on_http_exception,
            Exception: on_unexpected,
        },
    )
    app.state.archive = archive
    return app
