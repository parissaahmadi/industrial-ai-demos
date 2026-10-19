import json

import pytest
from pydantic import ValidationError

from event_archive.errors import Forbidden, Invalid, SourceRefConflict
from helpers import add, advance, audit_actions, audit_count, ev, make_archive


def test_ingest_stores_event_and_audits_it():
    a = make_archive()
    e, created = a.ingest(a.P["op.b"], ev(message="Pump tripped", tags=["trip", "bearing"]))
    assert created and e["id"] == 1
    assert (e["asset_id"], e["kind"], e["severity"], e["message"]) == ("PMP-0001", "alarm", "warning", "Pump tripped")
    assert e["tags"] == ["bearing", "trip"] and e["recording"] is None and e["redacted"] is False
    assert e["created_by"] == "op.b" and e["created_at"] == "2026-10-12T08:00:00Z"
    row = a.db.execute("SELECT * FROM audit").fetchone()
    assert (row["actor"], row["role"], row["action"], row["target"]) == ("op.b", "operator", "event.ingest", "event:1")


def test_audit_holds_a_fingerprint_not_the_message():
    a = make_archive()
    add(a, message="SECRET-MARKER-TEXT")
    dump = json.dumps([dict(r) for r in a.db.execute("SELECT * FROM audit")])
    assert "SECRET-MARKER-TEXT" not in dump and "message_sha256" in dump


def test_timestamps_are_stored_in_utc():
    a = make_archive()
    assert add(a, occurred_at="2026-10-12T06:30:00+03:30")["occurred_at"] == "2026-10-12T03:00:00Z"
    assert add(a, occurred_at="2026-10-12T06:30:15.987Z")["occurred_at"] == "2026-10-12T06:30:15Z"


@pytest.mark.parametrize("bad", ["2026-10-12T06:30:00", "yesterday", "", "2026-13-01T00:00:00Z"])
def test_bad_timestamps_are_rejected(bad):
    a = make_archive()
    with pytest.raises(Invalid, match="occurred_at"):
        a.ingest(a.P["op.b"], ev(occurred_at=bad))
    assert audit_count(a) == 0


def test_future_events_beyond_the_skew_are_rejected():
    a = make_archive(future_skew=300)
    add(a, occurred_at="2026-10-12T08:05:00Z")  # exactly +300 s: allowed
    with pytest.raises(Invalid, match="future"):
        a.ingest(a.P["op.b"], ev(occurred_at="2026-10-12T08:05:01Z"))


def test_model_validation():
    for bad in [dict(asset_id="pump-1"), dict(asset_id="PMP-1"), dict(kind="fault"), dict(severity="high"),
                dict(message=""), dict(message="x" * 2001), dict(tags=["a b"]), dict(tags=["t"] * 11),
                dict(source="Bad Source"), dict(extra_field=1)]:  # fmt: skip
        with pytest.raises(ValidationError):
            ev(**bad)
    ev(message="x" * 2000, tags=["t"] * 10)
    assert ev(message="  padded  ").message == "padded"


def test_same_source_ref_and_content_is_a_replay():
    a = make_archive()
    first, c1 = a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1"))
    n = audit_count(a)
    again, c2 = a.ingest(a.P["op.c"], ev(source="plc-7", source_ref="r1"))
    assert (c1, c2) == (True, False) and again["id"] == first["id"]
    assert audit_count(a) == n and a.stats(a.P["view.a"])["events"] == 1


def test_replay_ignores_tag_order_and_duplicates():
    a = make_archive()
    a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1", tags=["b", "a"]))
    _, created = a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1", tags=["a", "b", "a"]))
    assert created is False


@pytest.mark.parametrize("change", [dict(message="different"), dict(severity="critical"), dict(kind="event"),
                                    dict(asset_id="PMP-0002"), dict(tags=["other"]),
                                    dict(occurred_at="2026-10-12T06:00:01Z")])  # fmt: skip
def test_same_source_ref_with_different_content_conflicts(change):
    a = make_archive()
    first, _ = a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1"))
    with pytest.raises(SourceRefConflict) as e:
        a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1", **change))
    assert e.value.extra["event_id"] == first["id"]


def test_source_ref_is_scoped_by_source_and_optional():
    a = make_archive()
    a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1"))
    _, created = a.ingest(a.P["op.b"], ev(source="plc-8", source_ref="r1", message="other source"))
    assert created
    a.ingest(a.P["op.b"], ev())
    _, created = a.ingest(a.P["op.b"], ev())  # no ref: nothing to deduplicate on
    assert created and a.stats(a.P["view.a"])["events"] == 4


@pytest.mark.parametrize("who", ["view.a", "aud.e"])
def test_roles_that_cannot_ingest_are_refused_and_the_attempt_is_audited(who):
    a = make_archive()
    with pytest.raises(Forbidden):
        a.ingest(a.P[who], ev())
    assert a.stats(a.P["view.a"])["events"] == 0
    row = a.db.execute("SELECT * FROM audit").fetchone()
    assert (row["actor"], row["action"], row["target"]) == (who, "access.denied", "ingest")


@pytest.mark.parametrize("who", ["op.b", "adm.d"])
def test_operator_and_admin_may_ingest(who):
    a = make_archive()
    assert add(a, who)["created_by"] == who


def test_failed_ingest_leaves_no_event_and_no_audit_row():
    a = make_archive()

    def boom(_):
        raise RuntimeError("index failure")

    a._reindex = boom
    with pytest.raises(RuntimeError):
        a.ingest(a.P["op.b"], ev())
    assert a.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert audit_count(a) == 0
    assert audit_actions(a) == []


def test_clock_is_injected_not_read():
    a = make_archive()
    advance(a, 3600)
    assert add(a)["created_at"] == "2026-10-12T09:00:00Z"


def test_the_database_itself_refuses_a_duplicate_source_ref():
    import sqlite3

    a = make_archive()
    a.ingest(a.P["op.b"], ev(source="plc-7", source_ref="r1"))
    with pytest.raises(sqlite3.IntegrityError):
        a.db.execute(
            "INSERT INTO events (occurred_at, asset_id, kind, severity, message, source, source_ref, body_sha256, created_by,"
            " created_at) VALUES ('t', 'PMP-0001', 'note', 'info', 'm', 'plc-7', 'r1', 'x', 'u', 't')")
