"""Request bodies. ``extra="forbid"`` so a misspelt field is an error, not silently ignored."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

AssetId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}-\d{4}$")]
Tag = Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[^\s,;]+$")]
Source = Annotated[str, StringConstraints(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngestRequest(_Model):
    occurred_at: str = Field(description="ISO-8601 with an explicit UTC offset, e.g. 2026-10-12T06:30:00Z")
    asset_id: AssetId
    kind: Literal["alarm", "event", "note"]
    severity: Literal["info", "warning", "critical"] = "info"
    message: str = Field(min_length=1, max_length=2000)
    tags: list[Tag] = Field(default_factory=list, max_length=10)
    source: Source = "manual"
    source_ref: str | None = Field(default=None, min_length=1, max_length=80)


class CorrectRequest(_Model):
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=5000)


class RedactRequest(_Model):
    reason: str = Field(min_length=3, max_length=300)


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601; naive timestamps are rejected (their meaning would be a guess)."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("not an ISO-8601 timestamp") from None
    if dt.tzinfo is None:
        raise ValueError("the timestamp needs an explicit UTC offset (e.g. 'Z' or '+03:30')")
    return dt.astimezone(UTC).replace(microsecond=0)


def fmt_timestamp(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
