import pytest
from event_archive.errors import Forbidden, Invalid
from event_archive.models import RedactRequest
from helpers import add, add_audio, audit_count, make_archive, work


def populated():
    a = make_archive()
    add(a)
    add_audio(a)
    work(a)
    a.search(a.P["view.a"], "pump")
    return a


def test_every_write_path_leaves_exactly_the_expected_trail():
    a = populated()
    actions = [r["action"] for r in a.db.execute("SELECT action FROM audit ORDER BY id")]
    assert actions == ["event.ingest", "event.ingest", "recording.attach", "transcription.done", "search"]


def test_auditor_reads_and_verifies():
    a = populated()
    page = a.audit_entries(a.P["aud.e"])
    assert [e["id"] for e in page["items"]] == [1, 2, 3, 4, 5] and page["next_after_id"] is None
    assert a.audit_verify(a.P["aud.e"])["ok"]


@pytest.mark.parametrize("who", ["view.a", "op.b"])
def test_others_cannot_read_the_audit_trail(who):
    a = populated()
    with pytest.raises(Forbidden):
        a.audit_entries(a.P[who])
    with pytest.raises(Forbidden):
        a.audit_verify(a.P[who])


def test_admin_may_read_the_audit_trail():
    a = populated()
    assert len(a.audit_entries(a.P["adm.d"])["items"]) == 5


def test_filters_and_paging():
    a = populated()
    p = a.P["aud.e"]
    assert [e["id"] for e in a.audit_entries(p, action="event.ingest")["items"]] == [1, 2]
    assert [e["id"] for e in a.audit_entries(p, actor="system:worker")["items"]] == [4]
    first = a.audit_entries(p, limit=2)
    assert [e["id"] for e in first["items"]] == [1, 2] and first["next_after_id"] == 2
    second = a.audit_entries(p, limit=2, after_id=first["next_after_id"])
    assert [e["id"] for e in second["items"]] == [3, 4] and second["next_after_id"] == 4
    last = a.audit_entries(p, limit=2, after_id=4)
    assert [e["id"] for e in last["items"]] == [5] and last["next_after_id"] is None
    with pytest.raises(Invalid):
        a.audit_entries(p, limit=0)


def test_reading_the_audit_trail_is_not_itself_logged():
    a = populated()
    n = audit_count(a)
    a.audit_entries(a.P["aud.e"])
    a.audit_verify(a.P["aud.e"])
    assert audit_count(a) == n


def test_verify_sees_tampering_through_the_service():
    a = populated()
    head = a.audit_verify(a.P["aud.e"])["head"]
    a.db.execute("DROP TRIGGER audit_no_update")
    a.db.execute("UPDATE audit SET actor = 'someone.else' WHERE id = 2")
    r = a.audit_verify(a.P["aud.e"])
    assert (r["ok"], r["broken_at"]) == (False, 2)
    assert a.audit_verify(a.P["aud.e"], expected_head=head)["ok"] is False


def test_stats():
    a = populated()
    s = a.stats(a.P["aud.e"])  # any authenticated role
    assert s == {"events": 2, "by_kind": {"alarm": 2}, "by_severity": {"warning": 2},
                 "recordings": {"done": 1}, "audit_entries": 5}  # fmt: skip


def test_denied_attempts_are_recorded_even_though_the_action_fails():
    a = make_archive()
    with pytest.raises(Forbidden):
        a.redact(a.P["op.b"], 1, RedactRequest(reason="because"))
    row = a.db.execute("SELECT actor, role, action, target, detail FROM audit").fetchone()
    assert tuple(row) == ("op.b", "operator", "access.denied", "redact", '{"role":"operator"}')
