import json

import pytest
from helpers import NEW, TOKENS, as_, make_app


def flow_client():
    _, c = make_app()
    return {u: as_(c, u) for u in TOKENS} | {"anon": c.as_user(None)}


def create(c, **kw):
    r = c.post("/requests", {**NEW, **kw})
    assert r.status == 201, r.json
    return r.json


def post_action(c, rid, action, version, **kw):
    return c.post(f"/requests/{rid}/{action}", {"version": version, **kw})


def decide(c, rid, version, action, comment=None):
    body = {"version": version, "action": action}
    if comment:
        body["comment"] = comment
    return c.post(f"/requests/{rid}/decision", body)


# ------------------------------------------------------------------ plumbing
def test_health_needs_no_token_and_sets_security_headers():
    r = flow_client()["anon"].get("/health")
    assert r.status == 200 and r.json["status"] == "ok"
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_me():
    u = flow_client()
    assert u["rev.c"].get("/me").json == {"username": "rev.c", "display_name": "Sample Reviewer C", "role": "reviewer"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer"}, {"authorization": "Bearer "}, {"authorization": "Basic abc"},
     {"authorization": "Bearer not-a-real-token"}, {"authorization": "bearer"}],
)
def test_authentication_failures_are_401_with_a_challenge(headers):
    u = flow_client()
    r = u["anon"].get("/me", headers=headers, token=None)
    assert r.status == 401 and r.json["type"] == "unauthorized" and "Bearer" in r.headers["www-authenticate"]


def test_scheme_name_is_case_insensitive():
    u = flow_client()
    r = u["anon"].get("/me", headers={"authorization": f"bearer {TOKENS['sub.a']}"}, token=None)
    assert r.status == 200 and r.json["username"] == "sub.a"


def test_unknown_route_and_wrong_method_use_the_same_error_shape():
    u = flow_client()["sub.a"]
    r = u.get("/nothing-here")
    assert (r.status, r.json["type"]) == (404, "not_found")
    r = u.request("DELETE", "/requests")
    assert (r.status, r.json["type"]) == (405, "method_not_allowed")
    assert u.get("/requests/abc").status == 404  # ids must be integers


# ------------------------------------------------------------------ create & validation
def test_create_returns_201_location_and_the_request():
    r = flow_client()["sub.a"].post("/requests", NEW)
    assert r.status == 201 and r.headers["location"] == "/requests/1"
    j = r.json
    assert (j["status"], j["owner"], j["version"], j["revision"], j["needs_supervisor"]) == ("draft", "sub.a", 1, 1, False)


@pytest.mark.parametrize(
    "body,field",
    [
        ({**NEW, "title": "x"}, "title"),
        ({**NEW, "asset_id": "pump-3"}, "asset_id"),
        ({**NEW, "estimated_cost": -1}, "estimated_cost"),
        ({**NEW, "estimated_cost": "10"}, "estimated_cost"),  # strict: no silent str -> int
        ({**NEW, "estimated_cost": 1.5}, "estimated_cost"),
        ({**NEW, "estimated_cost": 10**11}, "estimated_cost"),
        ({**NEW, "priority": "asap"}, "priority"),
        ({**NEW, "estimated_cots": 5}, "estimated_cots"),  # typo: unknown field is an error, not ignored
        ({"title": "Replace hydraulic pump seal"}, "asset_id"),
    ],
)
def test_validation_errors_name_the_field(body, field):
    r = flow_client()["sub.a"].post("/requests", body)
    assert r.status == 422 and r.json["type"] == "validation_error"
    assert field in [e["field"] for e in r.json["errors"]]


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"text"', b"null", b""])
def test_malformed_bodies_are_422_not_500(raw):
    c = flow_client()["sub.a"]
    r = c.request("POST", "/requests", raw=raw, headers={"content-type": "application/json"})
    assert r.status == 422 and r.json["type"] == "validation_error" and c.crashed is None


def test_oversized_bodies_are_413():
    c = flow_client()["sub.a"]
    r = c.post("/requests", {**NEW, "description": "x" * 70_000})
    assert r.status == 413 and r.json["type"] == "payload_too_large"


def test_oversized_body_without_content_length_is_413_too():
    c = flow_client()["sub.a"]
    big = json.dumps({**NEW, "description": "x" * 70_000}).encode()
    r = c.request("POST", "/requests", raw=big, headers={"content-length": None})
    assert r.status == 413


def test_title_is_trimmed_and_unicode_survives():
    c = flow_client()["sub.a"]
    r = c.post("/requests", {**NEW, "title": "   تعویض آب‌بند پمپ هیدرولیک   ", "description": "شرح فارسی ✓"})
    assert r.status == 201 and r.json["title"] == "تعویض آب‌بند پمپ هیدرولیک" and r.json["description"] == "شرح فارسی ✓"


# ------------------------------------------------------------------ idempotency
def test_idempotency_key_replay_is_200_with_a_marker_header():
    c = flow_client()["sub.a"]
    h = {"idempotency-key": "retry-safe-key-1"}
    first = c.post("/requests", NEW, headers=h)
    again = c.post("/requests", NEW, headers=h)
    assert (first.status, again.status) == (201, 200)
    assert again.json["id"] == first.json["id"] and again.headers["idempotent-replayed"] == "true"
    assert len(c.get("/requests").json["items"]) == 1


def test_idempotency_key_misuse():
    c = flow_client()["sub.a"]
    h = {"idempotency-key": "retry-safe-key-2"}
    c.post("/requests", NEW, headers=h)
    r = c.post("/requests", {**NEW, "estimated_cost": 5}, headers=h)
    assert r.status == 422 and r.json["type"] == "idempotency_key_reused"
    r = c.post("/requests", NEW, headers={"idempotency-key": "bad key!"})
    assert r.status == 422 and "Idempotency-Key" in r.json["detail"]


# ------------------------------------------------------------------ full flow over HTTP
def test_costly_request_end_to_end():
    u = flow_client()
    rid = create(u["sub.a"], estimated_cost=80_000_000)["id"]
    r = post_action(u["sub.a"], rid, "submit", 1)
    assert r.status == 200 and r.json["status"] == "submitted" and r.json["needs_supervisor"] is True
    q = u["rev.c"].get("/queue").json
    assert [x["id"] for x in q["items"]] == [rid]
    r = decide(u["rev.c"], rid, 2, "approve")
    assert r.json["status"] == "reviewed" and r.json["reviewed_by"] == "rev.c"
    r = decide(u["rev.d"], rid, 3, "approve")
    assert r.status == 403 and "supervisor" in r.json["detail"]
    r = decide(u["sup.e"], rid, 3, "approve")
    assert r.status == 200 and r.json["status"] == "approved" and r.json["version"] == 4
    ev = u["sub.a"].get(f"/requests/{rid}/events").json["events"]
    assert [(e["actor"], e["action"]) for e in ev] == [("sub.a", "created"), ("sub.a", "submitted"),
                                                      ("rev.c", "reviewed"), ("sup.e", "approved")]  # fmt: skip


def test_return_edit_resubmit_over_http():
    u = flow_client()
    rid = create(u["sub.a"])["id"]
    post_action(u["sub.a"], rid, "submit", 1)
    r = decide(u["rev.c"], rid, 2, "return")
    assert r.status == 422 and "comment" in r.json["detail"]
    r = decide(u["rev.c"], rid, 2, "return", "Please add the asset tag number.")
    assert r.json["status"] == "returned"
    r = u["sub.a"].patch(f"/requests/{rid}", {"version": 3, "title": "Replace seal on pump P-10"})
    assert r.status == 200 and r.json["title"] == "Replace seal on pump P-10"
    r = post_action(u["sub.a"], rid, "submit", 4, comment="Added the tag.")
    assert (r.json["status"], r.json["revision"]) == ("submitted", 2)


def test_withdraw():
    u = flow_client()
    rid = create(u["sub.a"])["id"]
    r = post_action(u["sub.a"], rid, "withdraw", 1, comment="Raised twice by mistake.")
    assert r.json["status"] == "withdrawn"
    r = post_action(u["sub.a"], rid, "withdraw", 2)
    assert r.status == 409 and r.json["type"] == "invalid_transition"


# ------------------------------------------------------------------ error mapping
def test_status_codes_for_the_main_failure_classes():
    u = flow_client()
    rid = create(u["sub.a"])["id"]
    assert u["sub.b"].get(f"/requests/{rid}").status == 404  # someone else's draft
    assert u["rev.c"].get(f"/requests/{rid}").status == 404  # drafts are private even from reviewers
    assert u["sub.a"].get("/requests/999").status == 404
    post_action(u["sub.a"], rid, "submit", 1)
    assert decide(u["sub.a"], rid, 2, "approve").status == 403  # owner cannot decide
    assert decide(u["rev.c"], rid, 1, "approve").status == 409  # stale
    assert decide(u["rev.c"], rid, 2, "maybe").status == 422  # not a decision
    assert u["rev.c"].patch(f"/requests/{rid}", {"version": 2, "title": "Different title"}).status == 403
    assert u["sub.a"].patch(f"/requests/{rid}", {"version": 2, "title": "Different title"}).status == 409


def test_stale_version_response_carries_the_current_version():
    u = flow_client()
    rid = create(u["sub.a"])["id"]
    post_action(u["sub.a"], rid, "submit", 1)
    r = decide(u["rev.c"], rid, 1, "approve")
    assert r.status == 409 and r.json["type"] == "stale_version" and r.json["current_version"] == 2


# ------------------------------------------------------------------ lists
def test_list_filters_and_paging_over_http():
    u = flow_client()
    for _ in range(3):
        create(u["sub.a"])
    r = u["sub.a"].get("/requests?limit=2")
    assert r.json["total"] == 3 and len(r.json["items"]) == 2 and r.json["next_offset"] == 2
    assert u["sub.a"].get("/requests?status=draft&mine=true").json["total"] == 3
    assert u["sub.a"].get("/requests?status=approved").json["total"] == 0
    assert u["rev.c"].get("/requests").json["total"] == 0  # reviewers do not see drafts
    for bad in ("limit=0", "limit=1000", "limit=abc", "offset=-1", "status=almost", "mine=maybe"):
        r = u["sub.a"].get(f"/requests?{bad}")
        assert r.status == 422, bad


def test_listing_never_includes_foreign_drafts():
    u = flow_client()
    create(u["sub.a"])
    assert u["sub.b"].get("/requests").json["total"] == 0
    assert u["sup.e"].get("/queue").json["total"] == 0


# ------------------------------------------------------------------ robustness
def test_unexpected_errors_are_500_and_leak_nothing():
    flow, c = make_app()
    u = as_(c, "sub.a")

    def boom(*a, **k):
        raise RuntimeError("secret: /srv/data/passwords.txt")

    flow.get = boom
    r = u.get("/requests/1")
    assert r.status == 500 and r.json["type"] == "internal_error"
    assert "secret" not in r.raw.decode() and "passwords" not in r.raw.decode()


def test_all_responses_are_json_with_the_documented_error_shape():
    u = flow_client()
    for r in (u["anon"].get("/me", token=None), u["sub.a"].get("/nope"), u["sub.a"].post("/requests", {}),
              u["sub.a"].get("/requests/42")):  # fmt: skip
        j = r.json
        assert set(j) >= {"type", "title", "status", "detail"} and j["status"] == r.status
        json.dumps(j)
