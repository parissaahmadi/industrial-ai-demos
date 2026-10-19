"""HTTP layer (Starlette). Thin: authenticate, parse, call ``Service``, map errors."""

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
from .errors import Invalid, RateLimited, TokenServiceError, TooLarge, Unauthorized
from .models import CreateSession, TokenRequest
from .service import Principal, Service

log = logging.getLogger("livekit_tokens")
MAX_BODY = 4 * 1024
# Responses carry bearer credentials (LiveKit tokens): nothing may cache them.
SECURITY_HEADERS = {"cache-control": "no-store", "pragma": "no-cache", "x-content-type-options": "nosniff"}


def problem(status: int, code: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"type": code, "title": code.replace("_", " "), "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def ok(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def _svc(request: Request) -> Service:
    return request.app.state.service


async def _principal(request: Request) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("send 'Authorization: Bearer <token>'")
    return await run_in_threadpool(_svc(request).authenticate, token.strip())


async def _body(request: Request, cls: type[BaseModel]):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise TooLarge(f"request body is larger than {MAX_BODY} bytes")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            raise TooLarge(f"request body is larger than {MAX_BODY} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return cls.model_validate({}) if cls is TokenRequest else _invalid("a JSON body is required")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Invalid("the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise Invalid("the request body must be a JSON object")
    return cls.model_validate(data)


def _invalid(detail: str):
    raise Invalid(detail)


async def health(request: Request) -> Response:
    return ok({"status": "ok", "version": __version__})


async def me(request: Request) -> Response:
    p = await _principal(request)
    return ok({"username": p.username, "display_name": p.display_name, "role": p.role})


async def create_session(request: Request) -> Response:
    p = await _principal(request)
    body = await _body(request, CreateSession)
    s = await run_in_threadpool(_svc(request).create_session, p, body.asset_id, body.reason)
    return ok(s, 201, {"location": f"/sessions/{s['id']}"})


async def list_sessions(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_svc(request).list_sessions, p))


async def get_session(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_svc(request).get_session, p, request.path_params["session_id"]))


async def close_session(request: Request) -> Response:
    p = await _principal(request)
    return ok(await run_in_threadpool(_svc(request).close_session, p, request.path_params["session_id"]))


async def issue_token(request: Request) -> Response:
    p = await _principal(request)
    body = await _body(request, TokenRequest)
    return ok(await run_in_threadpool(_svc(request).issue_token, p, request.path_params["session_id"], body.ttl_seconds))


async def audit(request: Request) -> Response:
    p = await _principal(request)
    qp = request.query_params

    def num(name, default):
        raw = qp.get(name)
        if raw is None:
            return default
        if not raw.isdigit():
            raise Invalid(f"query parameter {name!r} must be a non-negative integer")
        return int(raw)

    return ok(await run_in_threadpool(lambda: _svc(request).audit_entries(p, after_id=num("after_id", 0), limit=num("limit", 50))))


async def on_service_error(request: Request, exc: TokenServiceError) -> Response:
    headers = {}
    if isinstance(exc, Unauthorized):
        headers["www-authenticate"] = 'Bearer realm="livekit-tokens"'
    if isinstance(exc, RateLimited):
        headers["retry-after"] = str(exc.extra["retry_after"])
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


def create_app(service: Service) -> Starlette:
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/me", me, methods=["GET"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions", list_sessions, methods=["GET"]),
            Route("/sessions/{session_id:int}", get_session, methods=["GET"]),
            Route("/sessions/{session_id:int}/token", issue_token, methods=["POST"]),
            Route("/sessions/{session_id:int}/close", close_session, methods=["POST"]),
            Route("/audit", audit, methods=["GET"]),
        ],
        exception_handlers={
            TokenServiceError: on_service_error,
            ValidationError: on_validation_error,
            HTTPException: on_http_exception,
            Exception: on_unexpected,
        },
    )
    app.state.service = service
    return app
