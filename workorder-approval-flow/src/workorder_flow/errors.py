"""Domain errors. ``status`` is the HTTP status the API layer answers with; ``code`` is stable and
machine-readable. Messages are written to be shown to the person who made the call."""

from __future__ import annotations


class FlowError(Exception):
    status = 400
    code = "error"

    def __init__(self, detail: str, **extra) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class Unauthorized(FlowError):
    status = 401
    code = "unauthorized"


class Forbidden(FlowError):
    status = 403
    code = "forbidden"


class NotFound(FlowError):
    status = 404
    code = "not_found"


class Invalid(FlowError):
    status = 422
    code = "validation_error"


class InvalidTransition(FlowError):
    status = 409
    code = "invalid_transition"


class StaleVersion(FlowError):
    status = 409
    code = "stale_version"


class IdempotencyConflict(FlowError):
    status = 422
    code = "idempotency_key_reused"


class TooLarge(FlowError):
    status = 413
    code = "payload_too_large"
