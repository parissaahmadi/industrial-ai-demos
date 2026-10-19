import json

import pytest

from event_archive.transcribe import MEDIA_TYPE, make_fakevox
from helpers import GOOD_AUDIO, TOKENS, as_, make_app

BODY = {"occurred_at": "2026-10-12T06:00:00Z", "asset_id": "PMP-0001", "kind": "alarm", "severity": "warning",
        "message": "Bearing temperature above 75 C", "tags": ["bearing"]}  # fmt: skip


def users():
    arch, c = make_app()
    return arch, {u: as_(c, u) for u in TOKENS} | {"anon": c.as_user(None)}


def put_audio(c, eid, audio=GOOD_AUDIO, ctype=MEDIA_TYPE, **headers):
    return c.request("PUT", f"/events/{eid}/recording", raw=audio, headers={"content-type": ctype, **headers})


# ------------------------------------------------------------------ plumbing
def test_health_needs_no_token_and_sets_security_headers():
    _, u = users()
    r = u["anon"].get("/health")
    assert r.status == 200 and r.json["status"] == "ok"
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_me_and_authentication_errors():
    _, u = users()
    assert u["op.b"].get("/me").json == {"username": "op.b", "display_name": "Sample Operator B", "role": "operator"}
    assert u["op.b"].get("/me").json["role"] == "operator"
    r = u["anon"].get("/me")
    assert r.status == 401 and 'Bearer realm="event-archive"' in r.headers["www-authenticate"]
    assert r.json["type"] == "unauthorized"
    assert u["anon"].get("/me", headers={"authorization": "Basic abc"}).status == 401
    assert u["anon"].get("/me", headers={"authorization": "Bearer "}).status == 401
    assert u["anon"].get("/me", headers={"authorization": "Bearer wrong"}).status == 401


def test_unknown_route_and_wrong_method_use_the_problem_shape():
    _, u = users()
    r = u["op.b"].get("/nope")
    assert (r.status, r.json["type"]) == (404, "not_found")
    r = u["op.b"].get("/events")
    assert (r.status, r.json["type"]) == (405, "method_not_allowed")
    r = u["op.b"].get("/events/abc")
    assert r.status == 404


def test_unexpected_errors_become_a_generic_500():
    arch, u = users()
    arch.stats = lambda p: 1 / 0
    r = u["op.b"].get("/stats")
    assert r.status == 500 and r.json["type"] == "internal_error"
    assert "division" not in r.raw.decode() and "ZeroDivision" not in r.raw.decode()


# ------------------------------------------------------------------ ingest
def test_ingest_replay_and_conflict():
    _, u = users()
    r = u["op.b"].post("/events", {**BODY, "source": "plc-7", "source_ref": "r1"})
    assert r.status == 201 and r.headers["location"] == f"/events/{r.json['id']}"
    again = u["op.c"].post("/events", {**BODY, "source": "plc-7", "source_ref": "r1"})
    assert again.status == 200 and again.headers["idempotent-replayed"] == "true" and again.json["id"] == r.json["id"]
    clash = u["op.b"].post("/events", {**BODY, "source": "plc-7", "source_ref": "r1", "message": "different"})
    assert (clash.status, clash.json["type"], clash.json["event_id"]) == (409, "source_ref_conflict", r.json["id"])


def test_ingest_validation_errors_name_the_field():
    _, u = users()
    r = u["op.b"].post("/events", {**BODY, "asset_id": "bad", "severity": "high"})
    assert r.status == 422 and r.json["type"] == "validation_error"
    assert {e["field"] for e in r.json["errors"]} == {"asset_id", "severity"}
    r = u["op.b"].post("/events", {**BODY, "occurred_at": "2026-10-12T06:00:00"})
    assert (r.status, r.json["type"]) == (422, "validation_error") and "occurred_at" in r.json["detail"]
    r = u["op.b"].post("/events", {**BODY, "surprise": 1})
    assert r.status == 422


def test_ingest_body_must_be_a_json_object_within_the_size_limit():
    _, u = users()
    c = u["op.b"]
    assert c.post("/events", raw=b"{oops").status == 422
    assert c.post("/events", raw=b"[1]").status == 422
    assert c.post("/events", raw=b"\xff\xfe").status == 422
    big = json.dumps({**BODY, "message": "x" * 70_000}).encode()
    assert c.post("/events", raw=big).status == 413
    assert c.post("/events", raw=big, headers={"content-length": None}).status == 413  # chunked: no declared length


def test_viewer_gets_403_with_the_problem_shape():
    _, u = users()
    r = u["view.a"].post("/events", BODY)
    assert (r.status, r.json["type"]) == (403, "forbidden")


def test_get_event():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    assert u["view.a"].get(f"/events/{eid}").json["message"] == BODY["message"]
    assert u["view.a"].get("/events/999").status == 404
    assert u["aud.e"].get(f"/events/{eid}").status == 403
    assert u["anon"].get(f"/events/{eid}").status == 401


# ------------------------------------------------------------------ search
def test_search_parameters_map_to_the_service():
    _, u = users()
    for i, (kind, sev) in enumerate([("alarm", "critical"), ("note", "info"), ("alarm", "info")]):
        u["op.b"].post("/events", {**BODY, "kind": kind, "severity": sev, "message": f"pump number {i}",
                                    "occurred_at": f"2026-10-12T06:0{i}:00Z"})  # fmt: skip
    v = u["view.a"]
    assert [i["message"] for i in v.get("/events/search?q=pump").json["items"]] == ["pump number 2", "pump number 1", "pump number 0"]
    assert v.get("/events/search?kind=alarm&severity=info").json["count"] == 1
    assert v.get("/events/search?q=pump&limit=1").json["next_cursor"]
    assert v.get("/events/search?q=pump&sort=relevance&limit=1").json["next_offset"] == 1
    assert v.get("/events/search?since=2026-10-12T06:01:00Z&until=2026-10-12T06:02:00Z").json["count"] == 1
    assert v.get("/events/search?q=pump&in=message").json["count"] == 3
    assert v.get("/events/search?has_recording=false").json["count"] == 3


def test_search_cursor_roundtrip_over_http():
    _, u = users()
    for i in range(5):
        u["op.b"].post("/events", {**BODY, "message": f"m{i}", "occurred_at": f"2026-10-12T06:0{i}:00Z"})
    seen, url = [], "/events/search?limit=2"
    while url:
        r = u["view.a"].get(url).json
        seen += [i["message"] for i in r["items"]]
        url = f"/events/search?limit=2&cursor={r['next_cursor']}" if r["next_cursor"] else None
    assert seen == ["m4", "m3", "m2", "m1", "m0"]


@pytest.mark.parametrize("qs", ["limit=abc", "limit=0", "offset=x", "has_recording=maybe", "kind=zzz", "sort=x", "in=meta",
                                "q=***", "cursor=!!!", "since=nope"])  # fmt: skip
def test_search_rejects_bad_parameters(qs):
    _, u = users()
    r = u["view.a"].get("/events/search?" + qs)
    assert (r.status, r.json["type"]) == (422, "validation_error")


def test_search_needs_a_token_and_read_permission():
    _, u = users()
    assert u["anon"].get("/events/search?q=x").status == 401
    assert u["aud.e"].get("/events/search?q=x").status == 403


def test_hostile_query_string_over_http():
    _, u = users()
    u["op.b"].post("/events", BODY)
    r = u["view.a"].get("/events/search?q=%22%20OR%201%3D1%20--")
    assert r.status == 200 and r.json["items"] == []


# ------------------------------------------------------------------ recordings, transcripts
def test_recording_upload_and_status_codes():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    r = put_audio(u["op.b"], eid)
    assert r.status == 201 and r.json["recording"]["status"] == "pending"
    again = put_audio(u["op.c"], eid)
    assert again.status == 200 and again.headers["idempotent-replayed"] == "true"
    assert put_audio(u["op.b"], eid, make_fakevox([("other", 0.9)])).status == 409
    assert put_audio(u["op.b"], 999).status == 404
    assert put_audio(u["view.a"], eid).status == 403
    assert put_audio(u["anon"], eid).status == 401


def test_recording_upload_validation():
    arch, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    assert put_audio(u["op.b"], eid, ctype="audio/wav").status == 415
    assert put_audio(u["op.b"], eid, audio=b"not audio at all").status == 422
    arch.max_audio = 50
    assert put_audio(u["op.b"], eid).status == 413
    assert put_audio(u["op.b"], eid, **{"content-length": None}).status == 413  # chunked upload
    assert arch.db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0


def test_worker_transcript_correction_over_http():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    put_audio(u["op.b"], eid, make_fakevox([("leek", 0.5)], "en"))
    assert u["view.a"].get(f"/events/{eid}/transcript").json["status"] == "pending"
    assert u["view.a"].post("/worker/run").status == 403
    assert u["op.b"].post("/worker/run").json["done"] == 1
    t = u["view.a"].get(f"/events/{eid}/transcript").json
    assert (t["text"], t["needs_review"], t["version"]) == ("leek", True, 1)
    r = u["op.b"].post(f"/events/{eid}/transcript/correct", {"version": 1, "text": "leak"})
    assert r.status == 200 and r.json["text"] == "leak" and r.json["version"] == 2
    stale = u["op.b"].post(f"/events/{eid}/transcript/correct", {"version": 1, "text": "x"})
    assert (stale.status, stale.json["type"], stale.json["current_version"]) == (409, "stale_version", 2)
    assert u["view.a"].post(f"/events/{eid}/transcript/correct", {"version": 2, "text": "x"}).status == 403
    assert u["op.b"].post(f"/events/{eid}/transcript/correct", {"version": 2}).status == 422
    assert u["view.a"].get("/events/search?q=leak&in=transcript").json["count"] == 1


def test_retry_endpoint():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    put_audio(u["op.b"], eid, make_fakevox([("x", 0.9)], fail="permanent"))
    assert u["op.b"].post(f"/events/{eid}/transcript/retry").status == 409  # not failed yet
    u["op.b"].post("/worker/run")
    r = u["op.b"].post(f"/events/{eid}/transcript/retry")
    assert r.status == 200 and r.json["recording"]["status"] == "pending"
    assert u["view.a"].post(f"/events/{eid}/transcript/retry").status == 403


def test_redact_endpoint():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    assert u["op.b"].post(f"/events/{eid}/redact", {"reason": "personal data"}).status == 403
    assert u["adm.d"].post(f"/events/{eid}/redact", {"reason": "x"}).status == 422
    r = u["adm.d"].post(f"/events/{eid}/redact", {"reason": "personal data"})
    assert r.status == 200 and r.json["message"] == "[redacted]"
    assert u["adm.d"].post(f"/events/{eid}/redact", {"reason": "personal data"}).status == 409


# ------------------------------------------------------------------ audit and stats
def test_audit_endpoints_and_roles():
    _, u = users()
    u["op.b"].post("/events", BODY)
    u["view.a"].post("/events", BODY)  # denied, audited
    for who in ("view.a", "op.b", "anon"):
        assert u[who].get("/audit").status in (401, 403)
        assert u[who].get("/audit/verify").status in (401, 403)
    denied = u["aud.e"].get("/audit?action=access.denied").json["items"]
    assert [(e["actor"], e["target"]) for e in denied] == [("view.a", "ingest"), ("view.a", "audit"), ("view.a", "audit"),
                                                           ("op.b", "audit"), ("op.b", "audit")]  # fmt: skip
    assert u["aud.e"].get("/audit?limit=1").json["next_after_id"] == 1
    assert u["aud.e"].get("/audit?limit=x").status == 422
    v = u["aud.e"].get("/audit/verify").json
    assert v["ok"] and v["count"] == 6  # 1 ingest + 5 denied attempts (a 401 has no identity to log)
    assert u["adm.d"].get("/audit/verify?expected_head=" + "0" * 64).json["ok"] is False


def test_stats_for_any_authenticated_role():
    _, u = users()
    u["op.b"].post("/events", BODY)
    for who in ("view.a", "aud.e", "adm.d"):
        assert u[who].get("/stats").json["events"] == 1
    assert u["anon"].get("/stats").status == 401


def test_audio_may_be_larger_than_a_json_body():
    _, u = users()
    eid = u["op.b"].post("/events", BODY).json["id"]
    big = make_fakevox([("x" * 1000, 0.9)] * 100)  # ~100 KB: far above the 64 KB JSON limit, below the audio limit
    assert len(big) > 64 * 1024
    assert put_audio(u["op.b"], eid, big).status == 201


def test_a_huge_declared_length_is_refused_before_any_of_the_body_is_read():
    import asyncio

    arch, _ = users()
    from event_archive.api import create_app

    app = create_app(arch)
    sent = []

    async def receive():
        raise AssertionError("the server started reading a body it should have refused")

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": "/events",
             "raw_path": b"/events", "query_string": b"", "client": ("t", 1), "server": ("t", 80), "scheme": "http",
             "root_path": "", "headers": [(b"authorization", f"Bearer {TOKENS['op.b']}".encode()),
                                          (b"content-length", b"50000000"), (b"content-type", b"application/json")]}  # fmt: skip
    asyncio.run(app(scope, receive, send))
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 413
