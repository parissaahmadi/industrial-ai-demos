"""Domain errors. ``status`` is the HTTP status the API answers with; ``code`` is stable and
machine-readable. Messages are written to be shown to the person who made the call."""

from __future__ import annotations


class ArchiveError(Exception):
    status = 400
    code = "error"

    def __init__(self, detail: str, **extra) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class Unauthorized(ArchiveError):
    status = 401
    code = "unauthorized"


class Forbidden(ArchiveError):
    status = 403
    code = "forbidden"


class NotFound(ArchiveError):
    status = 404
    code = "not_found"


class Invalid(ArchiveError):
    status = 422
    code = "validation_error"


class Conflict(ArchiveError):
    status = 409
    code = "conflict"


class StaleVersion(Conflict):
    code = "stale_version"


class SourceRefConflict(Conflict):
    code = "source_ref_conflict"


class RecordingConflict(Conflict):
    code = "recording_conflict"


class WrongState(Conflict):
    code = "wrong_state"


class TooLarge(ArchiveError):
    status = 413
    code = "payload_too_large"


class UnsupportedMedia(ArchiveError):
    status = 415
    code = "unsupported_media_type"
