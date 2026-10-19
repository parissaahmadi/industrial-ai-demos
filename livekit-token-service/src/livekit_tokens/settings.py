"""Configuration from environment variables. Refuses to start on weak or placeholder secrets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import ConfigError

MIN_SECRET_CHARS = 32
PLACEHOLDER_PREFIXES = ("change-me", "changeme", "your-", "example", "secret", "devsecret")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: bytes = field(repr=False)
    url: str
    session_seconds: int = 2 * 3600  # how long an assistance session can be joined at all
    default_ttl: int = 600  # token lifetime when the client does not ask
    min_ttl: int = 30
    max_ttl: int = 900
    rate_per_minute: int = 10  # token requests per user per minute

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def need(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        def number(name: str, default: int, low: int, high: int) -> int:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            if not raw.strip().isdigit() or not low <= int(raw) <= high:
                raise ConfigError(f"{name} must be an integer from {low} to {high}")
            return int(raw)

        key, secret, url = need("LIVEKIT_API_KEY"), need("LIVEKIT_API_SECRET"), need("LIVEKIT_URL")
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", key):
            raise ConfigError("LIVEKIT_API_KEY must be 3-64 letters, digits, '-' or '_'")
        if len(secret) < MIN_SECRET_CHARS:
            raise ConfigError(f"LIVEKIT_API_SECRET must be at least {MIN_SECRET_CHARS} characters")
        if secret.lower().startswith(PLACEHOLDER_PREFIXES) or len(set(secret)) < 8:
            raise ConfigError("LIVEKIT_API_SECRET looks like a placeholder; generate a random one (see README)")
        if not url.startswith(("ws://", "wss://")):
            raise ConfigError("LIVEKIT_URL must start with ws:// or wss://")
        s = cls(
            api_key=key,
            api_secret=secret.encode(),
            url=url,
            session_seconds=number("SESSION_SECONDS", 2 * 3600, 60, 24 * 3600),
            default_ttl=number("TOKEN_TTL_DEFAULT", 600, 30, 3600),
            min_ttl=number("TOKEN_TTL_MIN", 30, 10, 3600),
            max_ttl=number("TOKEN_TTL_MAX", 900, 30, 3600),
            rate_per_minute=number("RATE_PER_MINUTE", 10, 1, 600),
        )
        if not s.min_ttl <= s.default_ttl <= s.max_ttl:
            raise ConfigError("need TOKEN_TTL_MIN <= TOKEN_TTL_DEFAULT <= TOKEN_TTL_MAX")
        return s
