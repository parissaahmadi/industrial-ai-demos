"""HTTP layer (Starlette). Thin on purpose: authenticate, parse, call ``Flow``, map errors.

Every error leaves as ``{"type": code, "title": ..., "status": n, "detail": ...}`` (the shape of
RFC 9457 problem details). Unexpected exceptions are logged on the server and answered with a generic
500: internals never reach the client.
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
from .errors import FlowError, Invalid, TooLarge, Unauthorized
from .models import CreateRequest, Decision, EditRequest, VersionOnly
from .service import Flow, Principal

log = logging.getLogger("workorder_flow")
MAX_BODY = 64 * 1024
SECURITY_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


def problem(status: int, code: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"type": code, "title": code.replace("_", " "), "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def ok(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


# ----------------------------------------------------------------------------- helpers
def _flow(request: Request) -> Flow:
    return request.app.state.flow


async def _principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("send 'Authorization: Bearer <token>'")
    return await run_in_threadpool(_flow(request).authenticate, token.strip())


async def _json_body(request: Request) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise TooLarge(f"request body is larger than {MAX_BODY} bytes")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise TooLarge(f"request body is larger than {MAX_BODY} bytes")
    try:
        data = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        raise Invalid("the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise Invalid("the request body must be a JSON object")
    return data


async def _model(request: Request, cls: type[BaseModel]):
    return cls.model_validate(await _json_body(request))


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not raw.lstrip("-").isdigit():
        raise Invalid(f"query parameter {name!r} must be an integer")
    return int(raw)


def _bool_param(request: Request, name: str) -> bool:
    raw = request.query_params.get(name, "false").lower()
    if raw not in ("true", "false", "1", "0"):
        raise Invalid(f"query parameter {name!r} must be true or false")
    return raw in ("true", "1")


# ----------------------------------------------------------------------------- endpoints
async def health(request: Request) -> Response:
    return ok({"status": "ok", "version": __version__})


async def me(request: Request) -> Response:
    p = await _principal(request)
    return ok({"username": p.username, "display_name": p.display_name, "role": p.role})


async def create_request(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, CreateRequest)
    key = request.headers.get("idempotency-key")
    result, created = await run_in_threadpool(_flow(request).create, p, body, key)
    if created:
        return ok(result, 201, {"location": f"/requests/{result['id']}"})
    return ok(result, 200, {"location": f"/requests/{result['id']}", "idempotent-replayed": "true"})


async def list_requests(request: Request) -> Response:
    p = await _principal(request)
    result = await run_in_threadpool(
        lambda: _flow(request).list_requests(
            p, status=request.query_params.get("status"), mine=_bool_param(request, "mine"),
            limit=_int_param(request, "limit", 20), offset=_int_param(request, "offset", 0)))  # fmt: skip
    return ok(result)


async def queue(request: Request) -> Response:
    p = await _principal(request)
    result = await run_in_threadpool(
        lambda: _flow(request).queue(p, limit=_int_param(request, "limit", 20), offset=_int_param(request, "offset", 0)))
    return ok(result)


async def get_request(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_flow(request).get, p, request.path_params["request_id"]))


async def events(request: Request) -> Response:
    p = await _principal(request)
    return ok({"events": await run_in_threadpool(_flow(request).history, p, request.path_params["request_id"])})


async def edit_request(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, EditRequest)
    return ok(await run_in_threadpool(_flow(request).edit, p, request.path_params["request_id"], body))


async def submit_request(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, VersionOnly)
    return ok(await run_in_threadpool(_flow(request).submit, p, request.path_params["request_id"], body))


async def withdraw_request(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, VersionOnly)
    return ok(await run_in_threadpool(_flow(request).withdraw, p, request.path_params["request_id"], body))


async def decide_request(request: Request) -> Response:
    p = await _principal(request)
    body = await _model(request, Decision)
    return ok(await run_in_threadpool(_flow(request).decide, p, request.path_params["request_id"], body))


# ----------------------------------------------------------------------------- errors
async def on_flow_error(request: Request, exc: FlowError) -> Response:
    headers = {"www-authenticate": 'Bearer realm="workorder-flow"'} if isinstance(exc, Unauthorized) else None
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


def create_app(flow: Flow) -> Starlette:
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/me", me, methods=["GET"]),
            Route("/queue", queue, methods=["GET"]),
            Route("/requests", create_request, methods=["POST"]),
            Route("/requests", list_requests, methods=["GET"]),
            Route("/requests/{request_id:int}", get_request, methods=["GET"]),
            Route("/requests/{request_id:int}", edit_request, methods=["PATCH"]),
            Route("/requests/{request_id:int}/events", events, methods=["GET"]),
            Route("/requests/{request_id:int}/submit", submit_request, methods=["POST"]),
            Route("/requests/{request_id:int}/withdraw", withdraw_request, methods=["POST"]),
            Route("/requests/{request_id:int}/decision", decide_request, methods=["POST"]),
        ],
        exception_handlers={
            FlowError: on_flow_error,
            ValidationError: on_validation_error,
            HTTPException: on_http_exception,
            Exception: on_unexpected,
        },
    )
    app.state.flow = flow
    return app
