"""Shared test helpers."""

from livekit_tokens import db
from livekit_tokens.service import Principal, Service
from livekit_tokens.settings import Settings

START = 1_791_792_000  # 2026-10-12 08:00:00 UTC
SECRET = "k9Qw2Zr7Lm4Xv8Bn3Td6Hs1Jc5Fg0Yp2Aa9UeRt"
KEY = "plantkey"
ROLES = {u: r for u, _, r in db.SEED_USERS}
TOKENS = {u: f"test-api-token-{u}-0000000000" for u in ROLES}


class Clock:
    def __init__(self, t: float = START) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def settings(**over) -> Settings:
    base = dict(api_key=KEY, api_secret=SECRET.encode(), url="ws://localhost:7880")
    base.update(over)
    return Settings(**base)


def make_service(**over) -> Service:
    clock = Clock()
    counter = iter(range(1, 10_000))
    svc = Service(db.connect(), settings(**over), clock, room_suffix=lambda: f"r{next(counter):04d}")
    svc.seed_users()
    for user, token in TOKENS.items():
        svc.db.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                       (db.hash_token(token), user, START))  # fmt: skip
    svc.clock = clock
    svc.P = {u: Principal(u, d, r) for u, d, r in db.SEED_USERS}
    return svc


def open_session(svc, who="tech.a", asset="PMP-0001", reason="bearing noise"):
    return svc.create_session(svc.P[who], asset, reason)


def actions(svc):
    return [r["action"] for r in svc.db.execute("SELECT action FROM audit ORDER BY id")]
