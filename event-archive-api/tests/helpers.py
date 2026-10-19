"""Shared test helpers."""

from datetime import UTC, datetime

from event_archive import db
from event_archive.models import IngestRequest
from event_archive.service import Archive, Principal
from event_archive.transcribe import MEDIA_TYPE, make_fakevox

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
TOKENS = {u: f"test-token-{u}-0000000000" for u, _, _ in db.SEED_USERS}
ROLES = {u: r for u, _, r in db.SEED_USERS}
GOOD_AUDIO = make_fakevox([("bearing is hot", 0.95), ("stopping the pump", 0.95)], "en")


def make_archive(transcriber=None, **kw):
    box = {"now": NOW}
    conn = db.connect()
    for user, token in TOKENS.items():
        conn.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                     (db.hash_token(token), user, "2026-10-12T00:00:00Z"))  # fmt: skip
    arch = Archive(conn, lambda: box["now"], transcriber, **kw)
    arch.box = box
    arch.P = {u: Principal(u, f"Sample {u}", ROLES[u]) for u in TOKENS}
    return arch


def advance(arch, seconds):
    from datetime import timedelta

    arch.box["now"] += timedelta(seconds=seconds)


def ev(**over):
    body = dict(occurred_at="2026-10-12T06:00:00Z", asset_id="PMP-0001", kind="alarm", severity="warning",
                message="Bearing temperature above 75 C", tags=["bearing"])  # fmt: skip
    body.update(over)
    return IngestRequest(**body)


def add(arch, who="op.b", **over):
    event, _ = arch.ingest(arch.P[who], ev(**over))
    return event


def add_audio(arch, audio=GOOD_AUDIO, who="op.b", **over):
    event = add(arch, who, **over)
    arch.attach_recording(arch.P[who], event["id"], audio, MEDIA_TYPE)
    return event


def work(arch):
    return arch.run_worker()


def audit_actions(arch):
    return [r["action"] for r in arch.db.execute("SELECT action FROM audit ORDER BY id")]


def audit_count(arch):
    return arch.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]


def ids(result):
    return [i["id"] for i in result["items"]]


def make_app():
    from asgi import Client

    from event_archive.api import create_app

    arch = make_archive()
    return arch, Client(create_app(arch))


def as_(client, user):
    return client.as_user(TOKENS[user])
