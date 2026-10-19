"""Input validation (pydantic) and the JSON shapes the API returns."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_COST = 10_000_000_000  # 10 billion rials: a sanity bound, not a business rule

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
AssetId = Annotated[str, StringConstraints(pattern=r"^AST-\d{4}$")]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]
Priority = Literal["low", "medium", "high", "urgent"]
Version = Annotated[int, Field(ge=1)]

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class Strict(BaseModel):
    """Unknown fields are errors: a typo such as ``estimated_cots`` must not be silently ignored."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CreateRequest(Strict):
    title: Title
    description: Description = ""
    asset_id: AssetId
    priority: Priority = "medium"
    estimated_cost: Annotated[int, Field(ge=0, le=MAX_COST)]


class EditRequest(Strict):
    version: Version
    title: Title | None = None
    description: Description | None = None
    asset_id: AssetId | None = None
    priority: Priority | None = None
    estimated_cost: Annotated[int, Field(ge=0, le=MAX_COST)] | None = None


class VersionOnly(Strict):
    version: Version
    comment: Comment | None = None


class Decision(Strict):
    version: Version
    action: Literal["approve", "reject", "return"]
    comment: Comment | None = None


def valid_idempotency_key(key: str) -> bool:
    return bool(_IDEMPOTENCY_KEY.match(key))
