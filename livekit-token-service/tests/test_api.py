import json

from asgi import Client
from helpers import KEY, SECRET, TOKENS, make_service
from livekit_tokens import jwt
from livekit_tokens.api import MAX_BODY, create_app


def setup():
    svc = make_service()
    return svc, Client(create_app(svc))


def new_session(c, who="tech.a", **over):
    body = {"asset_id": "PMP-0001", "reason": "noise"}
    body.update(over)
    return c.post("/sessions", body, token=TOKENS[who])


# ------------------------------------------------------------------ plumbing
def test_health_needs_no_token():
    _, c = setup()
    r = c.get("/health", token=None)
    assert r.status == 200 and r.json["status"] == "ok"


def test_every_response_is_uncacheable():
    _, c = setup()
    for r in (c.get("/health", token=None), c.get("/me", token=None), new_session(c),
              c.get("/nope", token=None)):  # fmt: skip
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["pragma"] == "no-cache"


def test_missing_or_bad_credentials():
    _, c = setup()
    for hdr in (None, "", "Basic abc", "Bearer", "Bearer ", "Bearer wrong", "bearer"):
        r = c.get("/me", token=None, headers={"authorization": hdr} if hdr is not None else None)
        assert r.status == 401 and r.json["type"] == "unauthorized"
        assert r.headers["www-authenticate"].startswith("Bearer")


def test_scheme_is_case_insensitive():
    _, c = setup()
    assert c.get("/me", token=None, headers={"authorization": f"bearer {TOKENS['tech.a']}"}).status == 200


def test_me():
    _, c = setup()
    assert c.get("/me", token=TOKENS["exp.c"]).json == {"username": "exp.c", "display_name": "Expert C", "role": "expert"}


def test_unknown_route_and_wrong_method_are_json_problems():
    _, c = setup()
    r = c.get("/nope", token=None)
    assert r.status == 404 and r.json["type"] == "not_found"
    r = c.request("DELETE", "/sessions", token=TOKENS["tech.a"])
    assert r.status == 405 and r.json["type"] == "method_not_allowed" and "allow" in r.headers


def test_non_integer_session_id_is_a_404():
    _, c = setup()
    assert c.get("/sessions/abc", token=TOKENS["adm.e"]).status == 404
    assert c.post("/sessions/1x/token", token=TOKENS["adm.e"]).status == 404


# ------------------------------------------------------------------ sessions
def test_create_and_read_a_session():
    _, c = setup()
    r = new_session(c)
    assert r.status == 201 and r.headers["location"] == f"/sessions/{r.json['id']}"
    got = c.get(r.headers["location"], token=TOKENS["tech.a"])
    assert got.status == 200 and got.json == r.json
    assert c.get("/sessions", token=TOKENS["exp.c"]).json["items"][0]["id"] == r.json["id"]


def test_create_validation_errors():
    _, c = setup()
    for body in ({}, {"asset_id": "A"}, {"reason": "x"}, {"asset_id": 5, "reason": "x"},
                 {"asset_id": "A", "reason": "x", "extra": 1}, {"asset_id": None, "reason": "x"}):  # fmt: skip
        r = c.post("/sessions", body, token=TOKENS["tech.a"])
        assert r.status == 422 and r.json["type"] == "validation_error"
    r = new_session(c, asset_id="bad id")
    assert r.status == 422 and "asset_id" in r.json["detail"]


def test_bad_bodies():
    _, c = setup()
    t = TOKENS["tech.a"]
    for raw in (b"{oops", b"[1]", b"null", b'"x"', b"\xff\xfe"):
        r = c.post("/sessions", raw=raw, token=t)
        assert r.status == 422 and r.json["type"] == "validation_error", raw
    r = c.post("/sessions", token=t)  # no body at all
    assert r.status == 422


def test_second_open_session_is_409():
    _, c = setup()
    new_session(c)
    r = new_session(c)
    assert r.status == 409 and r.json["type"] == "session_exists" and r.json["session_id"] == 1


def test_forbidden_shapes():
    _, c = setup()
    r = new_session(c, who="exp.c")
    assert r.status == 403 and r.json["type"] == "forbidden"
    new_session(c)
    assert c.get("/sessions/1", token=TOKENS["tech.b"]).status == 403
    assert c.post("/sessions/1/close", token=TOKENS["tech.b"]).status == 403
    assert c.get("/sessions/99", token=TOKENS["tech.a"]).status == 404


def test_close_over_http():
    _, c = setup()
    new_session(c)
    r = c.post("/sessions/1/close", token=TOKENS["tech.a"])
    assert r.status == 200 and r.json["state"] == "closed"
    r = c.post("/sessions/1/token", token=TOKENS["exp.c"])
    assert r.status == 409 and r.json["type"] == "session_closed"


# ------------------------------------------------------------------ tokens
def test_issue_token_over_http():
    svc, c = setup()
    new_session(c)
    r = c.post("/sessions/1/token", token=TOKENS["exp.c"])
    assert r.status == 200
    assert set(r.json) == {"token", "url", "room", "identity", "role", "expires_at", "expires_in"}
    claims = jwt.decode(r.json["token"], SECRET.encode(), now=svc.clock(), issuer=KEY)
    assert claims["sub"] == "exp.c" and claims["video"]["room"] == r.json["room"]


def test_ttl_in_body():
    _, c = setup()
    new_session(c)
    assert c.post("/sessions/1/token", {"ttl_seconds": 120}, token=TOKENS["exp.c"]).json["expires_in"] == 120
    for bad in (5000, 5, "60", 60.5, True, None):
        r = c.post("/sessions/1/token", {"ttl_seconds": bad}, token=TOKENS["exp.c"])
        if bad is None:
            assert r.status == 200  # explicit null = default
        else:
            assert r.status == 422, bad
    assert c.post("/sessions/1/token", {"ttl": 60}, token=TOKENS["exp.c"]).status == 422  # typo is not ignored


def test_token_body_is_optional_but_must_be_an_object_when_present():
    _, c = setup()
    new_session(c)
    assert c.post("/sessions/1/token", token=TOKENS["exp.c"]).status == 200
    assert c.post("/sessions/1/token", raw=b"[]", token=TOKENS["exp.c"]).status == 422


def test_rate_limit_answers_429_with_retry_after():
    svc = make_service(rate_per_minute=2)
    c = Client(create_app(svc))
    new_session(c)
    for _ in range(2):
        assert c.post("/sessions/1/token", token=TOKENS["exp.c"]).status == 200
    r = c.post("/sessions/1/token", token=TOKENS["exp.c"])
    assert r.status == 429 and r.json["type"] == "rate_limited"
    assert r.headers["retry-after"] == "60" and r.json["retry_after"] == 60


def test_expired_session():
    svc, c = setup()
    new_session(c)
    svc.clock.advance(7200)
    r = c.post("/sessions/1/token", token=TOKENS["exp.c"])
    assert r.status == 409 and r.json["type"] == "session_expired"


# ------------------------------------------------------------------ audit
def test_audit_over_http():
    _, c = setup()
    new_session(c)
    c.post("/sessions/1/token", token=TOKENS["exp.c"])
    r = c.get("/audit", token=TOKENS["adm.e"])
    assert r.status == 200 and [i["action"] for i in r.json["items"]] == ["session.create", "token.issue"]
    assert c.get("/audit?after_id=1&limit=1", token=TOKENS["adm.e"]).json["items"][0]["id"] == 2
    for q in ("limit=abc", "limit=0", "after_id=-1", "limit=1.5"):
        assert c.get(f"/audit?{q}", token=TOKENS["adm.e"]).status == 422, q
    assert c.get("/audit", token=TOKENS["exp.c"]).status == 403


def test_token_and_secret_never_appear_in_the_audit_response():
    _, c = setup()
    new_session(c)
    tok = c.post("/sessions/1/token", token=TOKENS["exp.c"]).json["token"]
    raw = c.get("/audit", token=TOKENS["adm.e"]).raw.decode()
    assert tok not in raw and SECRET not in raw


# ------------------------------------------------------------------ limits and failures
def test_declared_oversize_body_is_refused_before_reading():
    _, c = setup()
    r = c.post("/sessions", raw=b"x" * (MAX_BODY + 1), token=TOKENS["tech.a"])
    assert r.status == 413 and r.json["type"] == "payload_too_large"
    ok = json.dumps({"asset_id": "A", "reason": "x" * 100}).encode()
    assert len(ok) < MAX_BODY and c.post("/sessions", raw=ok, token=TOKENS["tech.a"]).status == 201


def test_chunked_oversize_body_is_refused_while_streaming():
    _, c = setup()
    r = c.request("POST", "/sessions", raw=b"x" * (MAX_BODY + 1), token=TOKENS["tech.a"],
                  headers={"content-length": None})  # fmt: skip
    assert r.status == 413


def test_body_exactly_at_the_limit_is_read_not_refused():
    _, c = setup()
    r = c.post("/sessions", raw=b" " * MAX_BODY, token=TOKENS["tech.a"])
    assert r.status == 422  # empty JSON, but not 413


def test_unexpected_errors_do_not_leak_details():
    svc, c = setup()
    new_session(c)

    def boom(*a, **k):
        raise RuntimeError("secret detail /etc/passwd")

    svc.issue_token = boom
    r = c.post("/sessions/1/token", token=TOKENS["exp.c"])
    assert r.status == 500 and r.json["type"] == "internal_error"
    assert "passwd" not in r.raw.decode() and "RuntimeError" not in r.raw.decode()


def test_body_limit_is_four_kib():
    _, c = setup()
    t = TOKENS["tech.a"]
    assert MAX_BODY == 4096
    assert c.post("/sessions", raw=b" " * 3000, token=t).status == 422  # read, then rejected as empty JSON
    assert c.post("/sessions", raw=b" " * 5000, token=t).status == 413
    assert c.request("POST", "/sessions", raw=b" " * 5000, token=t, headers={"content-length": None}).status == 413
