import json

import pytest

from event_archive.errors import (
    Forbidden,
    Invalid,
    NotFound,
    RecordingConflict,
    StaleVersion,
    TooLarge,
    UnsupportedMedia,
    WrongState,
)
from event_archive.models import CorrectRequest, RedactRequest
from event_archive.transcribe import (
    MEDIA_TYPE,
    MockTranscriber,
    PermanentError,
    TransientError,
    check_container,
    make_fakevox,
)
from helpers import (
    GOOD_AUDIO,
    add,
    add_audio,
    advance,
    audit_actions,
    audit_count,
    make_archive,
    work,
)


def rec(a, event):
    return a.get(a.P["view.a"], event["id"])["recording"]


def tr(a, event):
    return a.get_transcript(a.P["view.a"], event["id"])


# ------------------------------------------------------------------ the mock engine itself
def test_fakevox_roundtrip():
    audio = make_fakevox([("a b", 0.9), ("c", 0.7)], "en")
    t = MockTranscriber().transcribe(audio)
    assert (t.language, t.text, t.confidence) == ("en", "a b c", 0.8)
    assert [(s.start, s.end) for s in t.segments] == [(0.0, 2.0), (2.0, 4.0)]


@pytest.mark.parametrize("audio", [b"RIFF....", b"FAKEVOX1\n{broken", b"FAKEVOX1\n" + b"\xff\xfe",
                                   b'FAKEVOX1\n{"lang":"de","segments":[]}',
                                   b'FAKEVOX1\n{"lang":"fa","segments":[{"start":0,"end":1,"text":"x","conf":1.5}]}',
                                   b'FAKEVOX1\n{"lang":"fa","segments":[{"start":3,"end":1,"text":"x","conf":0.5}]}',
                                   b'FAKEVOX1\n{"lang":"fa"}', b"FAKEVOX1\n[]"])  # fmt: skip
def test_unusable_containers_are_permanent_errors(audio):
    with pytest.raises(PermanentError):
        check_container(audio)


def test_mock_failure_modes():
    m = MockTranscriber()
    flaky = make_fakevox([("x", 0.9)], fail_attempts=2)
    for _ in range(2):
        with pytest.raises(TransientError):
            m.transcribe(flaky)
    assert m.transcribe(flaky).text == "x"
    with pytest.raises(PermanentError):
        m.transcribe(make_fakevox([("x", 0.9)], fail="permanent"))


def test_silence_is_an_empty_transcript():
    t = MockTranscriber().transcribe(make_fakevox([], "fa"))
    assert (t.text, t.confidence) == ("", None)


# ------------------------------------------------------------------ attaching audio
def test_attach_creates_a_pending_recording():
    a = make_archive()
    e = add(a)
    out, created = a.attach_recording(a.P["op.b"], e["id"], GOOD_AUDIO, MEDIA_TYPE)
    assert created and out["recording"]["status"] == "pending" and out["recording"]["size"] == len(GOOD_AUDIO)
    assert audit_actions(a)[-1] == "recording.attach"
    assert "audio" not in json.dumps(out)


def test_attach_is_idempotent_for_identical_audio_and_conflicts_otherwise():
    a = make_archive()
    e = add(a)
    a.attach_recording(a.P["op.b"], e["id"], GOOD_AUDIO, MEDIA_TYPE)
    n = audit_count(a)
    _, created = a.attach_recording(a.P["op.c"], e["id"], GOOD_AUDIO, MEDIA_TYPE + "; codec=x")
    assert created is False and audit_count(a) == n
    with pytest.raises(RecordingConflict):
        a.attach_recording(a.P["op.b"], e["id"], make_fakevox([("other", 0.9)]), MEDIA_TYPE)


def test_attach_checks_media_type_size_magic_and_event():
    a = make_archive(max_audio=len(GOOD_AUDIO))
    e = add(a)
    op = a.P["op.b"]
    for ctype in (None, "", "audio/wav", "application/json"):
        with pytest.raises(UnsupportedMedia):
            a.attach_recording(op, e["id"], GOOD_AUDIO, ctype)
    with pytest.raises(TooLarge):
        a.attach_recording(op, e["id"], GOOD_AUDIO + b"x", MEDIA_TYPE)
    with pytest.raises(Invalid, match="FAKEVOX"):
        a.attach_recording(op, e["id"], b"x" * 20, MEDIA_TYPE)
    with pytest.raises(NotFound):
        a.attach_recording(op, 999, GOOD_AUDIO, MEDIA_TYPE)
    assert rec(a, e) is None
    a.attach_recording(op, e["id"], GOOD_AUDIO, MEDIA_TYPE.upper())  # media types are case-insensitive


def test_viewer_may_not_attach():
    a = make_archive()
    e = add(a)
    with pytest.raises(Forbidden):
        a.attach_recording(a.P["view.a"], e["id"], GOOD_AUDIO, MEDIA_TYPE)


# ------------------------------------------------------------------ worker: success and review flag
def test_worker_transcribes_and_records_everything():
    a = make_archive()
    e = add_audio(a)
    assert work(a) == {"claimed": 1, "done": 1, "retry": 0, "failed": 0, "dead": 0, "stale": 0}
    r, t = rec(a, e), tr(a, e)
    assert (r["status"], r["attempts"], r["language"], r["confidence"]) == ("done", 1, "en", 0.95)
    assert t["text"] == "bearing is hot stopping the pump" and t["needs_review"] is False and t["version"] == 1
    last = a.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert (last["actor"], last["role"], last["action"]) == ("system:worker", "system", "transcription.done")
    assert "stopping" not in last["detail"]  # a fingerprint, not the text


def test_nothing_to_do_is_a_no_op():
    a = make_archive()
    n = audit_count(a)
    assert work(a)["claimed"] == 0 and audit_count(a) == n


@pytest.mark.parametrize("segments, flagged", [
    ([("x", 0.85)], False),               # average exactly at the threshold: not flagged
    ([("x", 0.849)], True),
    ([("x", 0.99)] * 3 + [("y", 0.5)], True),    # good average, one very poor segment
    ([("x", 0.6)] + [("y", 1.0)] * 9, False),   # segment exactly at its threshold: not flagged
    ([("x", 0.59)] + [("y", 1.0)] * 9, True),
    ([], False),
])  # fmt: skip
def test_review_flag(segments, flagged):
    a = make_archive()
    e = add_audio(a, make_fakevox(segments))
    work(a)
    assert tr(a, e)["needs_review"] is flagged


def test_worker_limit_and_order():
    a = make_archive()
    es = [add_audio(a, make_fakevox([(f"clip {i}", 0.9)]), message=f"m{i}") for i in range(5)]
    assert work_n(a, 2)["claimed"] == 2
    assert [rec(a, e)["status"] for e in es] == ["done", "done", "pending", "pending", "pending"]


def work_n(a, n):
    return a.run_worker(None, n)


# ------------------------------------------------------------------ worker: failures and back-off
def test_transient_failure_backs_off_exponentially_then_succeeds():
    a = make_archive(backoff_base=30, backoff_cap=900)
    e = add_audio(a, make_fakevox([("x", 0.9)], fail_attempts=2))
    assert work(a)["retry"] == 1
    row = a.db.execute("SELECT * FROM recordings").fetchone()
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == ("pending", 1, "2026-10-12T08:00:30Z")
    advance(a, 29)
    assert work(a)["claimed"] == 0  # not due yet
    advance(a, 1)
    assert work(a)["retry"] == 1
    assert a.db.execute("SELECT next_attempt_at FROM recordings").fetchone()[0] == "2026-10-12T08:01:30Z"  # +60 s
    advance(a, 60)
    assert work(a)["done"] == 1
    assert rec(a, e)["attempts"] == 3 and rec(a, e)["last_error"] is None
    assert [x for x in audit_actions(a) if x.startswith("transcription")] == ["transcription.retry"] * 2 + ["transcription.done"]


def test_backoff_is_capped():
    a = make_archive(backoff_base=100, backoff_cap=250, max_attempts=10)
    add_audio(a, make_fakevox([("x", 0.9)], fail_attempts=9))
    waits = []
    for _ in range(4):
        work(a)
        due = a.db.execute("SELECT next_attempt_at FROM recordings").fetchone()[0]
        waits.append(due[11:])
        a.box["now"] = a.box["now"].replace(hour=int(due[11:13]), minute=int(due[14:16]), second=int(due[17:19]))
    assert waits == ["08:01:40Z", "08:05:00Z", "08:09:10Z", "08:13:20Z"]  # 100, 200, then 250, 250


def test_attempts_run_out_and_the_job_dies():
    a = make_archive(max_attempts=3, backoff_base=1)
    e = add_audio(a, make_fakevox([("x", 0.9)], fail_attempts=99))
    for _ in range(3):
        work(a)
        advance(a, 10)
    r = rec(a, e)
    assert (r["status"], r["attempts"], r["last_error"]) == ("dead", 3, "engine busy (simulated)")
    assert work(a)["claimed"] == 0
    assert audit_actions(a).count("transcription.retry") == 2 and audit_actions(a)[-1] == "transcription.dead"


def test_permanent_failure_is_not_retried():
    a = make_archive()
    e = add_audio(a, make_fakevox([("x", 0.9)], fail="permanent"))
    assert work(a)["failed"] == 1
    assert (rec(a, e)["status"], rec(a, e)["attempts"]) == ("failed", 1)
    advance(a, 10_000)
    assert work(a)["claimed"] == 0


def test_damaged_payload_fails_in_the_worker_not_at_upload():
    a = make_archive()
    e = add_audio(a, b"FAKEVOX1\n{broken")
    assert work(a)["failed"] == 1
    assert rec(a, e)["last_error"] == "FAKEVOX payload is damaged"


class Exploding:
    def transcribe(self, audio):
        raise RuntimeError("secret detail: /etc/passwd")


def test_engine_bugs_are_retried_and_their_message_does_not_leak():
    a = make_archive(Exploding())
    e = add_audio(a)
    assert work(a)["retry"] == 1
    assert rec(a, e)["last_error"] == "engine error (RuntimeError)"
    assert "passwd" not in json.dumps([dict(r) for r in a.db.execute("SELECT * FROM audit")])


def test_manual_retry_requeues_failed_and_dead_only():
    a = make_archive()
    e = add_audio(a, make_fakevox([("x", 0.9)], fail="permanent"))
    with pytest.raises(WrongState):
        a.retry(a.P["op.b"], e["id"])  # still pending
    work(a)
    out = a.retry(a.P["op.b"], e["id"])
    assert out["recording"]["status"] == "pending" and out["recording"]["attempts"] == 0 and out["recording"]["last_error"] is None
    work(a)
    assert rec(a, e)["status"] == "failed"  # permanent means permanent
    with pytest.raises(Forbidden):
        a.retry(a.P["view.a"], e["id"])
    ok = add_audio(a)
    work(a)
    with pytest.raises(WrongState):
        a.retry(a.P["op.b"], ok["id"])  # done
    with pytest.raises(WrongState):
        a.retry(a.P["op.b"], add(a)["id"])  # no recording


def test_only_operators_and_admins_run_the_worker_by_hand():
    a = make_archive()
    for who in ("view.a", "aud.e"):
        with pytest.raises(Forbidden):
            a.run_worker(a.P[who])
    a.run_worker(a.P["op.b"])
    a.run_worker(a.P["adm.d"])


# ------------------------------------------------------------------ worker: crashes and races
def test_a_crashed_worker_is_recovered_after_its_lease_expires():
    a = make_archive(lease_seconds=120)
    e = add_audio(a)
    assert len(a._claim(10)) == 1  # a worker takes the job ... and dies
    assert rec(a, e)["status"] == "running"
    advance(a, 119)
    assert work(a)["claimed"] == 0  # lease still valid: do not steal
    advance(a, 1)
    assert work(a)["done"] == 1
    assert rec(a, e)["attempts"] == 2


def test_crash_loops_end_in_dead():
    a = make_archive(lease_seconds=10, max_attempts=2)
    e = add_audio(a)
    for _ in range(2):
        a._claim(10)  # claim, then "crash"
        advance(a, 10)
    assert work(a)["claimed"] == 0
    r = rec(a, e)
    assert (r["status"], r["attempts"], r["last_error"]) == ("dead", 2, "gave up: attempts exhausted")
    assert audit_actions(a)[-1] == "transcription.dead"


def test_a_late_result_from_a_replaced_worker_is_discarded():
    a = make_archive(lease_seconds=120)
    e = add_audio(a)

    def second_worker_takes_over():
        a._before_finish = None
        advance(a, 121)
        assert a.run_worker()["done"] == 1  # the lease expired, another worker finished the job

    a._before_finish = second_worker_takes_over
    assert work(a)["stale"] == 1
    assert rec(a, e)["attempts"] == 2 and rec(a, e)["status"] == "done"
    assert audit_actions(a).count("transcription.done") == 1


def test_redaction_during_transcription_wins():
    a = make_archive()
    e = add_audio(a, message="private words")
    a._before_finish = lambda: a.redact(a.P["adm.d"], e["id"], RedactRequest(reason="wrong asset"))
    assert work(a)["stale"] == 1
    assert rec(a, e)["status"] == "redacted"
    assert a.db.execute("SELECT transcript FROM recordings").fetchone()[0] is None
    assert a.db.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 0


# ------------------------------------------------------------------ corrections
def corr(a, e, text, version=None, who="op.b"):
    version = version or tr(a, e)["version"]
    return a.correct_transcript(a.P[who], e["id"], CorrectRequest(version=version, text=text))


def test_correction_replaces_text_keeps_original_and_clears_the_flag():
    a = make_archive()
    e = add_audio(a, make_fakevox([("nashat roghan", 0.5)], "fa"))
    work(a)
    assert tr(a, e)["needs_review"] is True
    out = corr(a, e, "  نشت   روغن\n")
    assert out["text"] == "نشت روغن" and out["original_text"] == "nashat roghan"
    assert (out["needs_review"], out["corrected"], out["version"]) == (False, True, 2)
    assert corr(a, e, "نشت شدید روغن")["original_text"] == "nashat roghan"  # still the machine's text
    assert a.db.execute("SELECT corrected_by FROM recordings").fetchone()[0] == "op.b"


def test_correction_updates_the_search_index():
    a = make_archive()
    e = add_audio(a, make_fakevox([("leek", 0.9)]))
    work(a)
    assert len(a.search(a.P["view.a"], "leek")["items"]) == 1
    corr(a, e, "leak")
    assert a.search(a.P["view.a"], "leek")["items"] == []
    assert len(a.search(a.P["view.a"], "leak", scope="transcript")["items"]) == 1


def test_correction_is_checked_for_state_version_and_role():
    a = make_archive()
    e = add_audio(a)
    with pytest.raises(WrongState):
        corr(a, e, "too early", version=1)  # not transcribed yet
    work(a)
    with pytest.raises(StaleVersion) as err:
        corr(a, e, "x", version=7)
    assert err.value.extra["current_version"] == 1
    with pytest.raises(Forbidden):
        corr(a, e, "x", who="view.a")
    corr(a, e, "x")
    with pytest.raises(StaleVersion):
        corr(a, e, "y", version=1)  # someone else was first
    with pytest.raises(WrongState):
        corr(a, add(a), "x", version=1)  # no recording at all


def test_correction_audit_has_hashes_not_text():
    a = make_archive()
    e = add_audio(a)
    work(a)
    corr(a, e, "CORRECTED-MARKER")
    row = a.db.execute("SELECT * FROM audit WHERE action = 'transcript.correct'").fetchone()
    d = json.loads(row["detail"])
    assert set(d) == {"before_sha256", "after_sha256", "version"} and d["version"] == 2
    assert "CORRECTED-MARKER" not in row["detail"] and d["before_sha256"] != d["after_sha256"]


def test_get_transcript_shapes():
    a = make_archive()
    e = add_audio(a)
    pending = tr(a, e)
    assert (pending["status"], pending["text"]) == ("pending", None)
    with pytest.raises(NotFound):
        a.get_transcript(a.P["view.a"], add(a)["id"])
    with pytest.raises(NotFound):
        a.get_transcript(a.P["view.a"], 999)


def test_a_late_result_is_discarded_even_while_the_replacement_is_still_running():
    a = make_archive(lease_seconds=120)
    e = add_audio(a)

    def replacement_claims_but_has_not_finished():
        a._before_finish = None
        advance(a, 121)
        assert len(a._claim(10)) == 1

    a._before_finish = replacement_claims_but_has_not_finished
    assert work(a)["stale"] == 1
    r = rec(a, e)
    assert (r["status"], r["attempts"]) == ("running", 2)  # the replacement still owns the job
    assert audit_actions(a).count("transcription.done") == 0


def test_a_dead_recording_can_be_retried():
    a = make_archive(max_attempts=1)
    e = add_audio(a, make_fakevox([("x", 0.9)], fail_attempts=1))
    work(a)
    assert rec(a, e)["status"] == "dead"
    out = a.retry(a.P["op.b"], e["id"])
    assert (out["recording"]["status"], out["recording"]["attempts"]) == ("pending", 0)
    assert work(a)["done"] == 1


def test_redaction_also_removes_the_machine_original_of_a_corrected_transcript():
    a = make_archive()
    e = add_audio(a)
    work(a)
    corr(a, e, "fixed text")
    assert a.db.execute("SELECT transcript_original FROM recordings").fetchone()[0] is not None
    a.redact(a.P["adm.d"], e["id"], RedactRequest(reason="personal data"))
    assert tuple(a.db.execute("SELECT transcript, transcript_original FROM recordings").fetchone()) == (None, None)
    assert a.get_transcript(a.P["view.a"], e["id"])["original_text"] is None
