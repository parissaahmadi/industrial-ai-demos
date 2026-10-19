"""Request bodies. Unknown fields are rejected, so a typo does not silently do nothing."""

from pydantic import BaseModel, ConfigDict, StrictInt


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    reason: str


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_seconds: StrictInt | None = None
