"""Domain errors. ``status`` is the HTTP status; ``code`` is stable and machine-readable."""

from __future__ import annotations


class TokenServiceError(Exception):
    status = 400
    code = "error"

    def __init__(self, detail: str, **extra) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class Unauthorized(TokenServiceError):
    status = 401
    code = "unauthorized"


class Forbidden(TokenServiceError):
    status = 403
    code = "forbidden"


class NotFound(TokenServiceError):
    status = 404
    code = "not_found"


class Invalid(TokenServiceError):
    status = 422
    code = "validation_error"


class Conflict(TokenServiceError):
    status = 409
    code = "conflict"


class SessionExists(Conflict):
    code = "session_exists"


class SessionClosed(Conflict):
    code = "session_closed"


class SessionExpired(Conflict):
    code = "session_expired"


class RateLimited(TokenServiceError):
    status = 429
    code = "rate_limited"


class TooLarge(TokenServiceError):
    status = 413
    code = "payload_too_large"


class ConfigError(Exception):
    """Bad or missing configuration; the process should refuse to start."""
