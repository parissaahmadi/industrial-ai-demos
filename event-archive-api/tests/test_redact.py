import json

import pytest
from pydantic import ValidationError

from event_archive.errors import Forbidden, NotFound, WrongState
from event_archive.models import RedactRequest
from event_archive.transcribe import MEDIA_TYPE
from helpers import GOOD_AUDIO, add, add_audio, audit_actions, make_archive, work

R = RedactRequest(reason="entered on the wrong asset")


def test_redact_removes_content_everywhere_but_keeps_a_tombstone():
    a = make_archive()
    e = add_audio(a, message="TOP-SECRET-WORDS about the pump", tags=["secret-tag"])
    work(a)
    out = a.redact(a.P["adm.d"], e["id"], R)
    assert (out["message"], out["tags"], out["redacted"]) == ("[redacted]", [], True)
    assert out["recording"]["status"] == "redacted"
    row = a.db.execute("SELECT audio, transcript, transcript_original FROM recordings").fetchone()
    assert tuple(row) == (None, None, None)
    assert a.db.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 0
    v = a.P["view.a"]
    for q in ("secret", "TOP-SECRET-WORDS", "stopping", "PMP-0001", "secret-tag"):
        assert a.search(v, q)["items"] == [], q
    got = a.get(v, e["id"])
    assert got["redacted"] and got["asset_id"] == "PMP-0001"  # metadata stays, content goes


def test_the_audit_trail_never_held_the_content_and_records_who_why_and_a_fingerprint():
    a = make_archive()
    e = add(a, message="TOP-SECRET-WORDS")
    a.redact(a.P["adm.d"], e["id"], RedactRequest(reason="personal data"))
    dump = json.dumps([dict(r) for r in a.db.execute("SELECT * FROM audit")])
    assert "TOP-SECRET-WORDS" not in dump
    d = json.loads(a.db.execute("SELECT detail FROM audit WHERE action = 'event.redact'").fetchone()[0])
    assert d["reason"] == "personal data" and len(d["message_sha256"]) == 64 and d["had_recording"] is False


@pytest.mark.parametrize("who", ["view.a", "op.b", "aud.e"])
def test_only_admins_redact(who):
    a = make_archive()
    e = add(a)
    with pytest.raises(Forbidden):
        a.redact(a.P[who], e["id"], R)
    assert a.get(a.P["view.a"], e["id"])["redacted"] is False
    assert audit_actions(a)[-1] == "access.denied"


def test_redact_twice_and_unknown_event():
    a = make_archive()
    e = add(a)
    a.redact(a.P["adm.d"], e["id"], R)
    with pytest.raises(WrongState):
        a.redact(a.P["adm.d"], e["id"], R)
    with pytest.raises(NotFound):
        a.redact(a.P["adm.d"], 999, R)


def test_redacted_events_take_no_recordings():
    a = make_archive()
    e = add(a)
    a.redact(a.P["adm.d"], e["id"], R)
    with pytest.raises(WrongState):
        a.attach_recording(a.P["op.b"], e["id"], GOOD_AUDIO, MEDIA_TYPE)


def test_redacted_event_is_still_listed_as_a_tombstone_without_content():
    a = make_archive()
    e = add(a, message="gone soon")
    a.redact(a.P["adm.d"], e["id"], R)
    item = a.search(a.P["view.a"])["items"][0]
    assert item["id"] == e["id"] and item["message"] == "[redacted]"


def test_reason_is_required():
    with pytest.raises(ValidationError):
        RedactRequest(reason="x")
