"""Builds the LiveKit access token (a signed JWT) for one participant of one room."""

from __future__ import annotations

from . import jwt
from .grants import VideoGrant
from .settings import Settings

NBF_BACKDATE = 5  # seconds: tolerates a media server whose clock is a little behind ours


def mint(settings: Settings, *, identity: str, name: str, grant: VideoGrant, now: int, exp: int,
         metadata: str = "", attributes: dict[str, str] | None = None) -> tuple[str, dict]:  # fmt: skip
    claims: dict = {
        "iss": settings.api_key,
        "sub": identity,
        "name": name,
        "nbf": now - NBF_BACKDATE,
        "exp": exp,
        "video": grant.to_claim(),
    }
    if metadata:
        claims["metadata"] = metadata
    if attributes:
        claims["attributes"] = attributes
    return jwt.encode(claims, settings.api_secret), claims
