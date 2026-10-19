"""Command line: init-db, serve, inspect-token, demo."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import UTC, datetime

from . import db, jwt
from .errors import ConfigError, TokenServiceError
from .service import Principal, Service
from .settings import Settings

DEMO_SECRET = "k9Qw2Zr7Lm4Xv8Bn3Td6Hs1Jc5Fg0Yp2Aa9UeRt"  # fictional, only used by `demo`
DEMO_START = int(datetime(2026, 10, 12, 8, 0, tzinfo=UTC).timestamp())


def _settings() -> Settings:
    return Settings.from_env(os.environ)


def _service(path: str, settings: Settings, now=time.time) -> Service:
    svc = Service(db.connect(path), settings, now)
    svc.seed_users()
    return svc


def cmd_init_db(args) -> int:
    svc = _service(args.db, _settings())
    print("API tokens for the fictional users (shown once; only their hashes are stored):")
    for username, _, role in db.SEED_USERS:
        print(f"  {username:8} {role:11} {svc.issue_api_token(username)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    svc = _service(args.db, _settings())
    uvicorn.run(create_app(svc), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_inspect(args) -> int:
    token = args.token or sys.stdin.read().strip()
    try:
        claims = jwt.unverified_claims(token)
        if args.verify:
            claims = jwt.decode(token, _settings().api_secret, now=time.time(), issuer=_settings().api_key)
    except (jwt.InvalidToken, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    note = "signature VERIFIED" if args.verify else "signature NOT checked (use --verify with LIVEKIT_API_SECRET set)"
    print(note)
    print(json.dumps(claims, indent=2, ensure_ascii=False))
    if isinstance(claims.get("exp"), int):
        left = claims["exp"] - int(time.time())
        print(f"expires in {left} s" if left > 0 else f"expired {-left} s ago")
    return 0


# --------------------------------------------------------------------------- demo
class _Clock:
    def __init__(self) -> None:
        self.t = float(DEMO_START)

    def __call__(self) -> float:
        return self.t


def _show(title: str) -> None:
    print(f"\n== {title}")


def _claims(token: str, secret: bytes, now: float) -> dict:
    return jwt.decode(token, secret, now=now, issuer="plantkey")


def cmd_demo(args) -> int:
    clock, rng = _Clock(), random.Random(7)
    settings = Settings(api_key="plantkey", api_secret=DEMO_SECRET.encode(), url="ws://localhost:7880")
    svc = Service(db.connect(), settings, clock, room_suffix=lambda: f"{rng.getrandbits(32):08x}")
    svc.seed_users()
    who = {u: Principal(u, d, r) for u, d, r in db.SEED_USERS}

    _show("1. a technician asks for remote help on a fictional pump")
    s = svc.create_session(who["tech.a"], "PMP-0001", "Bearing noise after restart, need a second pair of eyes")
    print(json.dumps(s, indent=2))

    _show("2. tokens: same room, different grants")
    toks = {}
    for user in ("tech.a", "exp.c", "sup.d"):
        r = svc.issue_token(who[user], s["id"])
        toks[user] = r["token"]
        c = _claims(r["token"], settings.api_secret, clock())
        v = c["video"]
        print(f"  {user:7} role={r['role']:10} expires_in={r['expires_in']}s publish={v['canPublish']!s:5} "
              f"sources={v.get('canPublishSources', [])} data={v['canPublishData']!s:5} hidden={v['hidden']}")  # fmt: skip
    print("  claims of the technician's token:")
    print(json.dumps(_claims(toks["tech.a"], settings.api_secret, clock()), indent=4))

    _show("3. who is refused")
    for label, fn in [
        ("another technician asks for a token", lambda: svc.issue_token(who["tech.b"], s["id"])),
        ("an expert tries to open a session", lambda: svc.create_session(who["exp.c"], "PMP-0001", "x")),
        ("a ttl of 1 hour", lambda: svc.issue_token(who["exp.c"], s["id"], ttl=3600)),
        ("the technician opens a second session", lambda: svc.create_session(who["tech.a"], "FAN-0003", "again")),
    ]:
        try:
            fn()
        except TokenServiceError as e:
            print(f"  {e.status} {e.code:17} {label}: {e.detail}")

    _show("4. a token is only as good as its signature and clock")
    t = toks["exp.c"]
    for label, secret, at in [("right secret, now", settings.api_secret, clock()),
                              ("wrong secret", b"x" * 40, clock()),
                              ("11 minutes later (ttl was 10)", settings.api_secret, clock() + 660)]:  # fmt: skip
        try:
            _claims(t, secret, at)
            print(f"  accepted   {label}")
        except jwt.InvalidToken as e:
            print(f"  rejected   {label}: {e}")

    _show("5. rate limit: 10 tokens per user per minute")
    got = 1  # the expert already took one
    try:
        for _ in range(12):
            svc.issue_token(who["exp.c"], s["id"], ttl=60)
            got += 1
    except TokenServiceError as e:
        print(f"  {got} tokens issued, then {e.status}: {e.detail}")

    _show("6. closing the session")
    clock.t += 120
    svc.close_session(who["tech.a"], s["id"])
    try:
        svc.issue_token(who["tech.a"], s["id"])
    except TokenServiceError as e:
        print(f"  new token: {e.status} {e.code}")
    c = _claims(toks["exp.c"], settings.api_secret, clock())
    print(f"  but the expert's earlier token is still valid for {c['exp'] - int(clock())} s more.")
    print("  (a self-hosted LiveKit server cannot revoke tokens: that is why they are short)")

    _show("7. audit trail (admin)")
    shown = None
    for e in svc.audit_entries(who["adm.e"], limit=100)["items"]:
        key = (e["actor"], e["action"], e["target"], json.dumps(e["detail"], sort_keys=True))
        if key == shown:  # collapse identical consecutive entries (the rate-limit loop)
            print(f"  #{e['id']:<2} (same as above)")
            continue
        shown = key
        print(f"  #{e['id']:<2} {key[0]:7} {key[1]:18} {key[2]:10} {key[3]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="livekit-tokens", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init-db", help="create the database and print one API token per fictional user")
    p.add_argument("--db", default="tokens.sqlite3")
    p.set_defaults(fn=cmd_init_db)
    p = sub.add_parser("serve", help="run the API (reads LIVEKIT_* settings from the environment)")
    p.add_argument("--db", default="tokens.sqlite3")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("inspect-token", help="show the claims of a LiveKit token")
    p.add_argument("token", nargs="?")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("demo", help="scripted, offline, fixed clock")
    p.set_defaults(fn=cmd_demo)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
