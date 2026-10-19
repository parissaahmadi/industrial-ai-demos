"""Minimal HS256 JWT (RFC 7519 / RFC 7515), standard library only.

Only what LiveKit access tokens need: sign, and verify with a strict algorithm check. Verification exists so
the tests (and the ``inspect-token`` command) can check what the service issues without trusting the code
that issued it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json


class InvalidToken(Exception):
    """The token is malformed, wrongly signed or not valid at the checked time."""


class ExpiredToken(InvalidToken):
    pass


class NotYetValid(InvalidToken):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not text or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in text):
        raise InvalidToken("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise InvalidToken("not base64url") from None


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode(claims: dict, secret: bytes) -> str:
    if not secret:
        raise ValueError("empty signing secret")
    signing_input = b64url(_json({"alg": "HS256", "typ": "JWT"})) + "." + b64url(_json(claims))
    sig = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + b64url(sig)


def _split(token: str) -> tuple[str, str, str]:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise InvalidToken("a JWT has three dot-separated parts")
    return parts[0], parts[1], parts[2]


def _load(part: str) -> dict:
    try:
        obj = json.loads(b64url_decode(part))
    except (ValueError, UnicodeDecodeError):
        raise InvalidToken("a part is not JSON") from None
    if not isinstance(obj, dict):
        raise InvalidToken("a part is not a JSON object")
    return obj


def unverified_claims(token: str) -> dict:
    """Claims WITHOUT checking the signature. For display only; never base a decision on it."""
    _, payload, _ = _split(token)
    return _load(payload)


def decode(token: str, secret: bytes, *, now: float, leeway: int = 0, issuer: str | None = None) -> dict:
    h, p, s = _split(token)
    header = _load(h)
    if header.get("alg") != "HS256":  # never trust the header to pick the algorithm ("none", RS256, ...)
        raise InvalidToken(f"unsupported algorithm {header.get('alg')!r}")
    expected = hmac.new(secret, f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, b64url_decode(s)):
        raise InvalidToken("bad signature")
    claims = _load(p)
    exp, nbf = claims.get("exp"), claims.get("nbf")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise InvalidToken("exp is required and must be an integer")
    if now >= exp + leeway:
        raise ExpiredToken("token expired")
    if nbf is not None and (not isinstance(nbf, int) or isinstance(nbf, bool)):
        raise InvalidToken("nbf must be an integer")
    if nbf is not None and now < nbf - leeway:
        raise NotYetValid("token not valid yet")
    if issuer is not None and claims.get("iss") != issuer:
        raise InvalidToken("wrong issuer")
    return claims
