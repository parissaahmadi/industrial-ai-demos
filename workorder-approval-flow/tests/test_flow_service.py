import pytest

from helpers import LIMIT, decide, make_flow, new_request, submit, submitted
from workorder_flow import models
from workorder_flow.errors import (
    Forbidden,
    IdempotencyConflict,
    Invalid,
    InvalidTransition,
    NotFound,
    StaleVersion,
    Unauthorized,
)


# ------------------------------------------------------------------ authentication
def test_authentication():
    f = make_flow()
    assert f.authenticate("test-token-rev.c-0000000000").role == "reviewer"
    for bad in (None, "", "nope", "test-token-rev.c-0000000001"):
        with pytest.raises(Unauthorized):
            f.authenticate(bad)


def test_tokens_are_stored_hashed_and_issued_ones_work_and_can_be_revoked():
    f = make_flow()
    tok = f.issue_token("sub.b")
    assert len(tok) >= 40 and f.authenticate(tok).username == "sub.b"
    stored = [r[0] for r in f.db.execute("SELECT token_sha256 FROM api_tokens")]
    assert tok not in stored and all(len(h) == 64 for h in stored)
    f.db.execute("UPDATE api_tokens SET revoked = 1")
    with pytest.raises(Unauthorized, match="revoked"):
        f.authenticate(tok)
    with pytest.raises(NotFound):
        f.issue_token("nobody")


def test_deactivated_users_lose_access_immediately():
    f = make_flow()
    f.db.execute("UPDATE users SET active = 0 WHERE username = 'rev.c'")
    with pytest.raises(Unauthorized):
        f.authenticate("test-token-rev.c-0000000000")
    with pytest.raises(NotFound):
        f.issue_token("rev.c")


# ------------------------------------------------------------------ happy paths
def test_small_request_is_finished_by_one_reviewer():
    f = make_flow()
    r = submitted(f, cost=LIMIT)
    assert r["status"] == "submitted" and r["needs_supervisor"] is False
    r = decide(f, r, "rev.c", "approve", "Looks right.")
    assert r["status"] == "approved" and r["next_step"].startswith("none")
    assert [e["action"] for e in f.history(f.P["sub.a"], r["id"])] == ["created", "submitted", "approved"]


def test_costly_request_needs_a_reviewer_and_then_a_different_supervisor():
    f = make_flow()
    r = submitted(f, cost=LIMIT + 1)
    assert r["needs_supervisor"] is True
    r = decide(f, r, "rev.c", "approve")
    assert (r["status"], r["reviewed_by"]) == ("reviewed", "rev.c")
    with pytest.raises(Forbidden, match="can only be decided by a supervisor"):
        decide(f, r, "rev.d", "approve")  # another reviewer is not enough
    r = decide(f, r, "sup.e", "approve")
    assert r["status"] == "approved"
    h = f.history(f.P["sup.e"], r["id"])
    assert [(e["actor"], e["to_status"]) for e in h] == [("sub.a", "draft"), ("sub.a", "submitted"),
                                                         ("rev.c", "reviewed"), ("sup.e", "approved")]  # fmt: skip


def test_a_supervisor_who_did_the_first_review_cannot_also_give_the_final_approval():
    f = make_flow()
    r = submitted(f, cost=LIMIT + 1)
    r = decide(f, r, "sup.e", "approve")  # supervisors may review at the first stage too
    assert r["status"] == "reviewed" and r["reviewed_by"] == "sup.e"
    with pytest.raises(Forbidden, match="already reviewed"):
        decide(f, r, "sup.e", "approve")
    assert decide(f, r, "sup.f", "approve")["status"] == "approved"


def test_return_edit_resubmit_cycle_bumps_the_revision_and_clears_the_reviewer():
    f = make_flow()
    r = submitted(f, cost=LIMIT + 1)
    r = decide(f, r, "rev.c", "approve")
    r = decide(f, r, "sup.e", "return", "Please split the work into two orders.")
    assert (r["status"], r["revision"], r["reviewed_by"]) == ("returned", 1, None)
    r = f.edit(f.P["sub.a"], r["id"], models.EditRequest(version=r["version"], estimated_cost=30_000_000))
    r = submit(f, r)
    assert (r["status"], r["revision"]) == ("submitted", 2) and r["needs_supervisor"] is False
    assert decide(f, r, "rev.c", "approve")["status"] == "approved"  # the earlier reviewer may review again
    assert [e["action"] for e in f.history(f.P["sub.a"], r["id"])] == [
        "created", "submitted", "reviewed", "returned", "edited", "resubmitted", "approved"]  # fmt: skip


def test_reject_and_withdraw():
    f = make_flow()
    r = decide(f, submitted(f), "rev.c", "reject", "Not needed this quarter.")
    assert r["status"] == "rejected"
    r2 = new_request(f)
    r2 = f.withdraw(f.P["sub.a"], r2["id"], models.VersionOnly(version=r2["version"], comment="Changed my mind."))
    assert r2["status"] == "withdrawn"


# ------------------------------------------------------------------ rules
def test_reject_and_return_need_a_comment():
    f = make_flow()
    r = submitted(f)
    for action in ("reject", "return"):
        for comment in (None, "", "no", "     "):
            with pytest.raises((Invalid, ValueError)):
                decide(f, r, "rev.c", action, comment)
    assert f.get(f.P["rev.c"], r["id"])["status"] == "submitted"  # nothing changed


def test_nobody_decides_their_own_request_even_with_the_right_role():
    f = make_flow()
    r = new_request(f, who="rev.c")
    r = submit(f, r, "rev.c")
    with pytest.raises(Forbidden, match="own request"):
        decide(f, r, "rev.c", "approve")
    r2 = submit(f, new_request(f, who="sup.e", cost=LIMIT + 1), "sup.e")
    with pytest.raises(Forbidden, match="own request"):
        decide(f, r2, "sup.e", "approve")
    assert decide(f, r2, "sup.f", "approve")["status"] == "reviewed"  # someone else can


def test_submitters_cannot_decide():
    f = make_flow()
    r = submitted(f)
    with pytest.raises(Forbidden, match="can only be decided by a reviewer or supervisor"):
        decide(f, r, "sub.a", "approve")  # the owner is a submitter
    with pytest.raises(NotFound):
        decide(f, r, "sub.b", "approve")  # another submitter cannot even see it


def test_only_the_owner_edits_submits_and_withdraws():
    f = make_flow()
    r = new_request(f)
    other = f.P["sub.b"]
    for call in (lambda: f.edit(other, r["id"], models.EditRequest(version=1, title="Hijacked title")),
                 lambda: f.submit(other, r["id"], models.VersionOnly(version=1)),
                 lambda: f.withdraw(other, r["id"], models.VersionOnly(version=1))):  # fmt: skip
        with pytest.raises(NotFound):  # someone else's draft does not exist for them
            call()
    s = submit(f, r)
    for call in (lambda: f.edit(f.P["rev.c"], s["id"], models.EditRequest(version=s["version"], title="Hijacked title")),
                 lambda: f.withdraw(f.P["sup.e"], s["id"], models.VersionOnly(version=s["version"]))):  # fmt: skip
        with pytest.raises(Forbidden, match="only the person who created"):
            call()


def test_editing_is_only_allowed_in_draft_and_returned():
    f = make_flow()
    r = submitted(f)
    with pytest.raises(InvalidTransition, match="cannot be edited"):
        f.edit(f.P["sub.a"], r["id"], models.EditRequest(version=r["version"], title="Another title"))
    with pytest.raises(InvalidTransition, match="cannot be submitted"):
        submit(f, r)


def test_final_requests_accept_nothing():
    f = make_flow()
    r = decide(f, submitted(f), "rev.c", "approve")
    with pytest.raises(InvalidTransition, match="final"):
        decide(f, r, "sup.e", "reject", "Too late to change.")
    with pytest.raises(InvalidTransition, match="cannot be withdrawn"):
        f.withdraw(f.P["sub.a"], r["id"], models.VersionOnly(version=r["version"]))
    with pytest.raises(InvalidTransition, match="cannot be submitted"):
        submit(f, r)


def test_empty_edit_is_rejected():
    f = make_flow()
    r = new_request(f)
    with pytest.raises(Invalid, match="nothing to change"):
        f.edit(f.P["sub.a"], r["id"], models.EditRequest(version=r["version"]))


# ------------------------------------------------------------------ concurrency
def test_stale_version_is_refused_and_reports_the_current_one():
    f = make_flow()
    r = submitted(f)
    stale = dict(r)
    decide(f, r, "rev.c", "return", "Please add the asset tag.")
    with pytest.raises(StaleVersion, match="version 2") as e:
        decide(f, stale, "rev.d", "approve")
    assert e.value.extra["current_version"] == 3


def test_two_reviewers_acting_on_the_same_copy_only_one_wins():
    f = make_flow()
    r = submitted(f)
    decide(f, r, "rev.c", "approve")
    with pytest.raises((StaleVersion, InvalidTransition)):
        decide(f, r, "rev.d", "reject", "I disagree strongly.")
    assert f.get(f.P["sub.a"], r["id"])["status"] == "approved"


def test_version_counts_every_change():
    f = make_flow()
    r = new_request(f)
    assert r["version"] == 1
    r = f.edit(f.P["sub.a"], r["id"], models.EditRequest(version=1, title="A better title"))
    r = submit(f, r)
    r = decide(f, r, "rev.c", "approve")
    assert r["version"] == 4 and [e["version"] for e in f.history(f.P["sub.a"], r["id"])] == [1, 2, 3, 4]


def test_authorisation_is_checked_before_staleness_so_nothing_leaks():
    f = make_flow()
    r = submitted(f)
    with pytest.raises(Forbidden):
        f.decide(f.P["sub.a"], r["id"], models.Decision(version=999, action="approve"))  # not StaleVersion
    with pytest.raises(NotFound):
        f.decide(f.P["sub.b"], r["id"], models.Decision(version=999, action="approve"))


# ------------------------------------------------------------------ visibility
def test_drafts_are_private_everything_else_is_visible_to_reviewers():
    f = make_flow()
    d = new_request(f)
    for who in ("sub.b", "rev.c", "sup.e"):
        with pytest.raises(NotFound):
            f.get(f.P[who], d["id"])
        with pytest.raises(NotFound):
            f.history(f.P[who], d["id"])
    s = submit(f, d)
    assert f.get(f.P["rev.c"], s["id"])["id"] == s["id"] and f.get(f.P["sup.e"], s["id"])["id"] == s["id"]
    with pytest.raises(NotFound):
        f.get(f.P["sub.b"], s["id"])  # another submitter still sees nothing
    with pytest.raises(NotFound, match="does not exist"):
        f.get(f.P["sub.a"], 9999)


def test_listing_respects_visibility_and_filters():
    f = make_flow()
    a1 = new_request(f, "sub.a")
    a2 = submit(f, new_request(f, "sub.a"))
    b1 = submit(f, new_request(f, "sub.b"), "sub.b")
    ids = lambda who, **kw: sorted(x["id"] for x in f.list_requests(f.P[who], **kw)["items"])  # noqa: E731
    assert ids("sub.a") == sorted([a1["id"], a2["id"]]) and ids("sub.b") == [b1["id"]]
    assert ids("rev.c") == sorted([a2["id"], b1["id"]])  # no foreign drafts
    assert ids("rev.c", mine=True) == []
    assert ids("sub.a", status="draft") == [a1["id"]]
    with pytest.raises(Invalid, match="status"):
        f.list_requests(f.P["sub.a"], status="almost")


def test_paging():
    f = make_flow()
    for _ in range(5):
        new_request(f)
    p1 = f.list_requests(f.P["sub.a"], limit=2)
    assert p1["total"] == 5 and len(p1["items"]) == 2 and p1["next_offset"] == 2
    p3 = f.list_requests(f.P["sub.a"], limit=2, offset=4)
    assert len(p3["items"]) == 1 and p3["next_offset"] is None
    seen = [x["id"] for off in (0, 2, 4) for x in f.list_requests(f.P["sub.a"], limit=2, offset=off)["items"]]
    assert seen == sorted(seen, reverse=True) and len(set(seen)) == 5
    for kw in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        with pytest.raises(Invalid):
            f.list_requests(f.P["sub.a"], **kw)


def test_queue_shows_only_what_the_caller_can_act_on():
    f = make_flow()
    draft = new_request(f, "sub.a")
    small = submitted(f, "sub.a")
    big = submitted(f, "sub.b", cost=LIMIT + 1)
    q = lambda who: sorted(x["id"] for x in f.queue(f.P[who])["items"])  # noqa: E731
    assert q("sub.a") == sorted([draft["id"]])  # own draft only: submitters cannot decide
    assert q("rev.c") == sorted([small["id"], big["id"]])
    own = submit(f, new_request(f, "rev.c"), "rev.c")
    assert own["id"] not in q("rev.c") and own["id"] in q("rev.d")
    reviewed = decide(f, big, "rev.c", "approve")
    assert reviewed["id"] not in q("rev.c") and reviewed["id"] not in q("rev.d")  # reviewers are done with it
    assert reviewed["id"] in q("sup.e") and reviewed["id"] in q("sup.f")
    r2 = decide(f, small, "sup.e", "return", "Please attach a quote.")
    assert r2["id"] not in q("sup.e") and r2["id"] in q("sub.a")  # now it waits for its owner
    sup_first = decide(f, submitted(f, "sub.b", cost=LIMIT + 1), "sup.e", "approve")
    assert sup_first["id"] not in q("sup.e") and sup_first["id"] in q("sup.f")  # four-eyes in the queue


# ------------------------------------------------------------------ idempotency
def body(**kw):
    base = dict(title="Replace hydraulic pump seal", asset_id="AST-0003", estimated_cost=1_000_000)
    return models.CreateRequest(**{**base, **kw})


def test_idempotent_create_returns_the_original_and_creates_nothing_new():
    f = make_flow()
    a, created_a = f.create(f.P["sub.a"], body(), "key-aaaaaaaa-1")
    b, created_b = f.create(f.P["sub.a"], body(), "key-aaaaaaaa-1")
    assert created_a and not created_b and a["id"] == b["id"]
    assert f.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert f.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1


def test_same_key_with_a_different_body_is_refused():
    f = make_flow()
    f.create(f.P["sub.a"], body(), "key-aaaaaaaa-2")
    with pytest.raises(IdempotencyConflict, match="different request body"):
        f.create(f.P["sub.a"], body(estimated_cost=2_000_000), "key-aaaaaaaa-2")


def test_keys_are_per_user_and_validated():
    f = make_flow()
    a, _ = f.create(f.P["sub.a"], body(), "shared-key-12345")
    b, created = f.create(f.P["sub.b"], body(), "shared-key-12345")
    assert created and a["id"] != b["id"]
    for bad in ("short", "has space here", "x" * 65, "semi;colon-1234"):
        with pytest.raises(Invalid, match="Idempotency-Key"):
            f.create(f.P["sub.a"], body(), bad)
    _, c1 = f.create(f.P["sub.a"], body())  # no key: always creates
    _, c2 = f.create(f.P["sub.a"], body())
    assert c1 and c2


# ------------------------------------------------------------------ audit & atomicity
def test_audit_log_is_append_only_in_the_database_itself():
    f = make_flow()
    submitted(f)
    for sql in ("UPDATE audit_events SET actor = 'mallory'", "DELETE FROM audit_events"):
        with pytest.raises(Exception, match="append-only"):
            f.db.execute(sql)
    assert f.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2


def test_a_failed_write_changes_nothing():
    f = make_flow()
    r = submitted(f)
    f.db.execute("CREATE TRIGGER boom BEFORE INSERT ON audit_events WHEN NEW.action = 'approved' "
                 "BEGIN SELECT RAISE(ABORT, 'disk on fire'); END")  # fmt: skip
    with pytest.raises(Exception, match="disk on fire"):
        decide(f, r, "rev.c", "approve")
    f.db.execute("DROP TRIGGER boom")
    again = f.get(f.P["rev.c"], r["id"])
    assert again["status"] == "submitted" and again["version"] == r["version"]  # UPDATE rolled back with the INSERT
    assert not f.db.in_transaction


def test_audit_timestamps_follow_the_injected_clock():
    from datetime import timedelta

    f = make_flow()
    r = new_request(f)
    f.box["now"] += timedelta(hours=3)
    submit(f, r)
    h = f.history(f.P["sub.a"], r["id"])
    assert (h[0]["at"], h[1]["at"]) == ("2026-10-12T06:00:00Z", "2026-10-12T09:00:00Z")


def test_reviewer_limit_is_configurable():
    f = make_flow(limit=1_000)
    r = decide(f, submitted(f, cost=1_001), "rev.c", "approve")
    assert r["status"] == "reviewed"
    f2 = make_flow(limit=10**9)
    assert decide(f2, submitted(f2, cost=LIMIT * 5), "rev.c", "approve")["status"] == "approved"
