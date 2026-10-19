import json

import pytest

from helpers import KEY, SECRET, START, Clock, actions, make_service, open_session
from livekit_tokens import jwt
from livekit_tokens.errors import (
    Forbidden,
    Invalid,
    NotFound,
    RateLimited,
    SessionClosed,
    SessionExists,
    SessionExpired,
    Unauthorized,
)


def claims(svc, result, at=None):
    return jwt.decode(result["token"], SECRET.encode(), now=svc.clock() if at is None else at, issuer=KEY)


# ------------------------------------------------------------------ opening a session
def test_technician_opens_a_session():
    svc = make_service()
    s = open_session(svc)
    assert s["state"] == "open" and s["created_by"] == "tech.a" and s["asset_id"] == "PMP-0001"
    assert s["room"] == "assist-r0001"
    assert s["created_at"] == "2026-10-12T08:00:00Z" and s["expires_at"] == "2026-10-12T10:00:00Z"
    assert actions(svc) == ["session.create"]


@pytest.mark.parametrize("who", ["exp.c", "sup.d", "adm.e"])
def test_only_technicians_open_sessions(who):
    svc = make_service()
    with pytest.raises(Forbidden):
        open_session(svc, who)
    assert actions(svc) == ["access.denied"]


@pytest.mark.parametrize("asset", ["", "  ", "-x", ".x", "a b", "a/b", "x" * 41, "پمپ", "A;DROP"])
def test_bad_asset_ids(asset):
    with pytest.raises(Invalid):
        open_session(make_service(), asset=asset)


@pytest.mark.parametrize("asset", ["P", "PMP-0001", "a.b_c-d", "x" * 40])
def test_good_asset_ids(asset):
    assert open_session(make_service(), asset=asset)["asset_id"] == asset


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201, "line\nbreak", "tab\there", "nul\x00"])
def test_bad_reasons(reason):
    with pytest.raises(Invalid):
        open_session(make_service(), reason=reason)


def test_reason_limits_and_persian_text():
    svc = make_service()
    assert open_session(svc, reason="x" * 200)["reason"] == "x" * 200
    svc2 = make_service()
    assert open_session(svc2, reason=" صدای یاتاقان ")["reason"] == "صدای یاتاقان"


def test_one_open_session_per_technician_but_not_across_technicians():
    svc = make_service()
    first = open_session(svc)
    with pytest.raises(SessionExists) as e:
        open_session(svc, asset="FAN-0003")
    assert e.value.extra["session_id"] == first["id"]
    assert open_session(svc, "tech.b")["id"] != first["id"]


def test_a_new_session_is_possible_after_closing_and_after_expiry():
    svc = make_service()
    a = open_session(svc)
    svc.close_session(svc.P["tech.a"], a["id"])
    b = open_session(svc)
    assert b["id"] != a["id"] and b["room"] != a["room"]
    svc.clock.advance(7200)  # exactly the session length: it is over
    assert open_session(svc)["id"] != b["id"]


def test_session_is_still_open_one_second_before_its_end():
    svc = make_service()
    s = open_session(svc)
    svc.clock.advance(7199)
    assert svc.get_session(svc.P["tech.a"], s["id"])["state"] == "open"
    svc.clock.advance(1)
    assert svc.get_session(svc.P["tech.a"], s["id"])["state"] == "expired"


def test_rooms_are_unguessable_by_default():
    from helpers import settings
    from livekit_tokens import db
    from livekit_tokens.service import Service

    svc = Service(db.connect(), settings(), Clock())
    svc.seed_users()
    rooms = set()
    for i in range(5):
        p = svc.authenticate(svc.issue_api_token("tech.a"))
        s = svc.create_session(p, "PMP-0001", "x")
        rooms.add(s["room"])
        svc.close_session(p, s["id"])
    assert len(rooms) == 5 and all(len(r) == len("assist-") + 16 for r in rooms)


# ------------------------------------------------------------------ visibility
def test_a_technician_sees_only_their_own_sessions():
    svc = make_service()
    a, b = open_session(svc, "tech.a"), open_session(svc, "tech.b")
    assert [s["id"] for s in svc.list_sessions(svc.P["tech.a"])["items"]] == [a["id"]]
    assert svc.get_session(svc.P["tech.a"], a["id"])["id"] == a["id"]
    with pytest.raises(Forbidden):
        svc.get_session(svc.P["tech.a"], b["id"])


@pytest.mark.parametrize("who", ["exp.c", "sup.d", "adm.e"])
def test_staff_see_open_sessions_only(who):
    svc = make_service()
    a, b = open_session(svc, "tech.a"), open_session(svc, "tech.b")
    svc.close_session(svc.P["tech.b"], b["id"])
    assert [s["id"] for s in svc.list_sessions(svc.P[who])["items"]] == [a["id"]]
    svc.clock.advance(7200)
    assert svc.list_sessions(svc.P[who])["items"] == []


def test_unknown_session():
    svc = make_service()
    for fn in (svc.get_session, svc.close_session, svc.issue_token):
        with pytest.raises(NotFound):
            fn(svc.P["adm.e"], 99)


# ------------------------------------------------------------------ closing
def test_owner_and_admin_can_close_others_cannot():
    svc = make_service()
    s = open_session(svc)
    for who in ("tech.b", "exp.c", "sup.d"):
        with pytest.raises(Forbidden):
            svc.close_session(svc.P[who], s["id"])
    closed = svc.close_session(svc.P["adm.e"], s["id"])
    assert closed["state"] == "closed" and closed["closed_by"] == "adm.e"
    s2 = open_session(svc)
    assert svc.close_session(svc.P["tech.a"], s2["id"])["closed_by"] == "tech.a"


def test_closing_twice_is_harmless_and_keeps_the_first_closer():
    svc = make_service()
    s = open_session(svc)
    svc.close_session(svc.P["tech.a"], s["id"])
    svc.clock.advance(10)
    again = svc.close_session(svc.P["adm.e"], s["id"])
    assert again["closed_by"] == "tech.a" and again["closed_at"] == "2026-10-12T08:00:00Z"
    assert actions(svc).count("session.close") == 1


def test_closed_session_state_wins_over_expired():
    svc = make_service()
    s = open_session(svc)
    svc.close_session(svc.P["tech.a"], s["id"])
    svc.clock.advance(99999)
    assert svc.get_session(svc.P["adm.e"], s["id"])["state"] == "closed"


# ------------------------------------------------------------------ tokens
@pytest.mark.parametrize("who,role,publish,sources,hidden", [
    ("tech.a", "technician", True, ["camera", "microphone"], False),
    ("exp.c", "expert", True, ["microphone", "camera", "screen_share"], False),
    ("sup.d", "observer", False, None, True),
    ("adm.e", "observer", False, None, True),
])  # fmt: skip
def test_each_role_gets_its_own_grant(who, role, publish, sources, hidden):
    svc = make_service()
    s = open_session(svc)
    r = svc.issue_token(svc.P[who], s["id"])
    c = claims(svc, r)
    v = c["video"]
    assert r["role"] == role and c["attributes"] == {"role": role}
    assert json.loads(c["metadata"]) == {"role": role, "asset": "PMP-0001"}
    assert v["room"] == s["room"] == r["room"] and v["roomJoin"] is True
    assert v["canPublish"] is publish and v["hidden"] is hidden and v.get("canPublishSources") == sources
    assert c["iss"] == KEY and c["sub"] == who == r["identity"] and c["name"] == svc.P[who].display_name


def test_another_technician_gets_nothing_and_it_is_audited():
    svc = make_service()
    s = open_session(svc)
    with pytest.raises(Forbidden):
        svc.issue_token(svc.P["tech.b"], s["id"])
    row = svc.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["actor"], row["action"]) == ("tech.b", "access.denied")


def test_times_and_default_ttl():
    svc = make_service()
    s = open_session(svc)
    r = svc.issue_token(svc.P["tech.a"], s["id"])
    c = claims(svc, r)
    assert c["exp"] == START + 600 and r["expires_in"] == 600 and r["expires_at"] == "2026-10-12T08:10:00Z"
    assert c["nbf"] == START - 5  # backdated a little against clock differences with the media server
    assert r["url"] == "ws://localhost:7880"


@pytest.mark.parametrize("ttl,ok", [(29, False), (30, True), (900, True), (901, False), (0, False), (-1, False),
                                    (True, False), (60.0, False), ("60", False)])  # fmt: skip
def test_ttl_bounds(ttl, ok):
    svc = make_service()
    s = open_session(svc)
    if ok:
        assert svc.issue_token(svc.P["tech.a"], s["id"], ttl)["expires_in"] == ttl
    else:
        with pytest.raises(Invalid):
            svc.issue_token(svc.P["tech.a"], s["id"], ttl)


def test_a_token_never_outlives_its_session():
    svc = make_service()
    s = open_session(svc)
    svc.clock.advance(7200 - 100)
    r = svc.issue_token(svc.P["exp.c"], s["id"], 600)
    assert r["expires_in"] == 100 and claims(svc, r)["exp"] == START + 7200


def test_no_token_in_the_last_instant_of_the_session():
    svc = make_service()
    s = open_session(svc)
    svc.clock.advance(7199)
    assert svc.issue_token(svc.P["exp.c"], s["id"])["expires_in"] == 1
    svc.clock.advance(1)
    with pytest.raises(SessionExpired):
        svc.issue_token(svc.P["exp.c"], s["id"])


def test_closed_session_gives_no_tokens_but_old_ones_stay_valid_until_they_expire():
    svc = make_service()
    s = open_session(svc)
    old = svc.issue_token(svc.P["exp.c"], s["id"])
    svc.close_session(svc.P["tech.a"], s["id"])
    for who in ("tech.a", "exp.c", "sup.d"):
        with pytest.raises(SessionClosed):
            svc.issue_token(svc.P[who], s["id"])
    claims(svc, old)  # documented limit: a signed token cannot be recalled
    svc.clock.advance(600)
    with pytest.raises(jwt.ExpiredToken):
        claims(svc, old)


def test_denied_wins_over_closed_so_outsiders_learn_nothing_about_state():
    svc = make_service()
    s = open_session(svc)
    svc.close_session(svc.P["tech.a"], s["id"])
    with pytest.raises(Forbidden):
        svc.issue_token(svc.P["tech.b"], s["id"])


def test_invalid_ttl_does_not_use_up_the_rate_limit():
    svc = make_service(rate_per_minute=2)
    s = open_session(svc)
    for _ in range(5):
        with pytest.raises(Invalid):
            svc.issue_token(svc.P["exp.c"], s["id"], 5000)
    svc.issue_token(svc.P["exp.c"], s["id"])
    svc.issue_token(svc.P["exp.c"], s["id"])


def test_rate_limit_per_user_and_recovery():
    svc = make_service(rate_per_minute=3)
    s = open_session(svc)
    for _ in range(3):
        svc.issue_token(svc.P["exp.c"], s["id"])
    with pytest.raises(RateLimited) as e:
        svc.issue_token(svc.P["exp.c"], s["id"])
    assert e.value.extra["retry_after"] == 60
    svc.issue_token(svc.P["sup.d"], s["id"])  # someone else is unaffected
    svc.clock.advance(60)
    svc.issue_token(svc.P["exp.c"], s["id"])
    assert "token.rate_limited" in actions(svc)


def test_denied_attempts_do_not_count_against_the_limit():
    svc = make_service(rate_per_minute=1)
    s = open_session(svc)
    for _ in range(4):
        with pytest.raises(Forbidden):
            svc.issue_token(svc.P["tech.b"], s["id"])


def test_two_tokens_for_one_user_are_both_valid_same_identity():
    svc = make_service()
    s = open_session(svc)
    a = svc.issue_token(svc.P["exp.c"], s["id"], 60)
    svc.clock.advance(1)
    b = svc.issue_token(svc.P["exp.c"], s["id"], 60)
    assert a["token"] != b["token"] and claims(svc, a)["sub"] == claims(svc, b)["sub"]


# ------------------------------------------------------------------ audit
def test_audit_never_contains_a_token_or_the_secret_or_the_reason():
    svc = make_service()
    s = open_session(svc, reason="very private reason text")
    r = svc.issue_token(svc.P["tech.a"], s["id"])
    dump = json.dumps([dict(x) for x in svc.db.execute("SELECT * FROM audit")])
    assert r["token"] not in dump and SECRET not in dump and "very private" not in dump
    assert r["token"].split(".")[2] not in dump


def test_audit_fingerprint_matches_the_token():
    import hashlib

    svc = make_service()
    s = open_session(svc)
    r = svc.issue_token(svc.P["tech.a"], s["id"])
    row = svc.db.execute("SELECT detail FROM audit WHERE action = 'token.issue'").fetchone()
    assert json.loads(row["detail"])["fingerprint"] == hashlib.sha256(r["token"].encode()).hexdigest()[:16]


def test_audit_is_append_only():
    import sqlite3

    svc = make_service()
    open_session(svc)
    with pytest.raises(sqlite3.DatabaseError):
        svc.db.execute("UPDATE audit SET actor = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        svc.db.execute("DELETE FROM audit")
    assert actions(svc) == ["session.create"]


def test_only_admin_reads_the_audit_and_a_refusal_is_itself_recorded():
    svc = make_service()
    open_session(svc)
    for who in ("tech.a", "exp.c", "sup.d"):
        with pytest.raises(Forbidden):
            svc.audit_entries(svc.P[who])
    items = svc.audit_entries(svc.P["adm.e"])["items"]
    assert [i["action"] for i in items] == ["session.create"] + ["access.denied"] * 3
    assert items[1]["detail"]["attempted"] == "audit.read"


def test_audit_paging_and_limits():
    svc = make_service()
    s = open_session(svc)
    for _ in range(3):
        svc.issue_token(svc.P["exp.c"], s["id"])
    first = svc.audit_entries(svc.P["adm.e"], limit=2)["items"]
    rest = svc.audit_entries(svc.P["adm.e"], after_id=first[-1]["id"], limit=50)["items"]
    assert [i["id"] for i in first + rest] == [1, 2, 3, 4]
    for bad in (0, 201):
        with pytest.raises(Invalid):
            svc.audit_entries(svc.P["adm.e"], limit=bad)
    assert len(svc.audit_entries(svc.P["adm.e"], limit=200)["items"]) == 4


# ------------------------------------------------------------------ API tokens
def test_api_token_authentication():
    svc = make_service()
    p = svc.authenticate("test-api-token-exp.c-0000000000")
    assert (p.username, p.role) == ("exp.c", "expert")
    for bad in ("", "nope", "test-api-token-exp.c-000000000"):
        with pytest.raises(Unauthorized):
            svc.authenticate(bad)


def test_issued_api_tokens_are_stored_hashed_and_work():
    svc = make_service()
    t = svc.issue_api_token("tech.a")
    assert len(t) >= 40 and svc.authenticate(t).username == "tech.a"
    stored = [r[0] for r in svc.db.execute("SELECT token_sha256 FROM api_tokens")]
    assert t not in stored and all(len(x) == 64 for x in stored)
    with pytest.raises(NotFound):
        svc.issue_api_token("ghost")


def test_ttl_true_is_not_the_number_one():
    svc = make_service(min_ttl=1)
    s = open_session(svc)
    assert svc.issue_token(svc.P["tech.a"], s["id"], 1)["expires_in"] == 1
    with pytest.raises(Invalid):
        svc.issue_token(svc.P["tech.a"], s["id"], True)
