"""Shared test helpers."""

from datetime import UTC, datetime

from workorder_flow import db, models, service

NOW = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
LIMIT = service.DEFAULT_REVIEWER_LIMIT  # 50,000,000 rials
TOKENS = {u: f"test-token-{u}-0000000000" for u, _, _ in db.SEED_USERS}


def make_flow(limit=LIMIT):
    box = {"now": NOW}
    conn = db.connect()
    for user, token in TOKENS.items():
        conn.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                     (db.hash_token(token), user, "2026-10-12T00:00:00Z"))  # fmt: skip
    flow = service.Flow(conn, lambda: box["now"], limit)
    flow.box = box
    flow.P = {u: flow.authenticate(t) for u, t in TOKENS.items()}  # principals by username
    return flow


def new_request(flow, who="sub.a", cost=10_000_000, **kw):
    body = dict(title="Replace hydraulic pump seal", asset_id="AST-0003", estimated_cost=cost, **kw)
    r, _ = flow.create(flow.P[who], models.CreateRequest(**body))
    return r


def submit(flow, r, who="sub.a"):
    return flow.submit(flow.P[who], r["id"], models.VersionOnly(version=r["version"]))


def decide(flow, r, who, action, comment=None):
    return flow.decide(flow.P[who], r["id"], models.Decision(version=r["version"], action=action, comment=comment))


def submitted(flow, who="sub.a", cost=10_000_000):
    return submit(flow, new_request(flow, who, cost), who)


def make_app(limit=LIMIT):
    from asgi import Client

    from workorder_flow.api import create_app

    flow = make_flow(limit)
    app = create_app(flow)
    return flow, Client(app)


def as_(client, user):
    return client.as_user(TOKENS[user])


NEW = {"title": "Replace hydraulic pump seal", "asset_id": "AST-0003", "estimated_cost": 10_000_000}
