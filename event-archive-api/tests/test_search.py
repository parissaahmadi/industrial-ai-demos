import json
from datetime import UTC, datetime

import pytest
from event_archive.errors import Forbidden, Invalid
from event_archive.sample_data import sample_events
from helpers import add, add_audio, audit_count, ids, make_archive, work

V = "view.a"


def search(a, q=None, **kw):
    return a.search(a.P[V], q, **kw)


def loaded():
    a = make_archive()
    a.box["now"] = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)  # after the last sample event
    for e in sample_events():
        a.ingest(a.P["op.b"], e)
    return a


# ------------------------------------------------------------------ matching
def test_words_are_anded_and_case_insensitive():
    a = make_archive()
    one = add(a, message="Bearing temperature high")
    add(a, message="Bearing noise")
    assert ids(search(a, "BEARING temperature")) == [one["id"]]


def test_phrase_must_be_adjacent():
    a = make_archive()
    hit = add(a, message="high bearing temperature")
    add(a, message="bearing has a high temperature")
    assert ids(search(a, '"bearing temperature"')) == [hit["id"]]
    assert len(search(a, "bearing temperature")["items"]) == 2


def test_prefix_search():
    a = make_archive()
    hit = add(a, message="abnormal vibration")
    assert ids(search(a, "vibr*")) == [hit["id"]]
    assert search(a, "vibr")["items"] == []  # no star, no prefix


def test_persian_spelling_variants_and_digits_find_each_other():
    a = make_archive()
    arabic = add(a, message="موتور فن خنك‌كننده بازرسی شد")  # Arabic kaf, ZWNJ
    assert ids(search(a, "خنک کننده")) == [arabic["id"]]
    assert ids(search(a, "خنک‌کننده")) == [arabic["id"]]
    plate = add(a, message="شماره ۱۲۳۴ ثبت شد")
    assert ids(search(a, "1234")) == [plate["id"]]
    assert ids(search(a, "١٢٣٤")) == [plate["id"]]


def test_asset_id_and_tags_are_searchable_but_kind_and_severity_words_are_not():
    a = make_archive()
    e = add(a, asset_id="PMP-0001", message="x", tags=["vibration"])
    assert ids(search(a, "PMP-0001")) == [e["id"]]
    assert ids(search(a, "pmp-۰۰۰۱")) == [e["id"]]
    assert ids(search(a, "vibration")) == [e["id"]]
    assert search(a, "alarm")["items"] == [] and search(a, "warning")["items"] == []


def test_scope_message_vs_transcript():
    a = make_archive()
    e = add_audio(a, message="pump alarm")
    work(a)
    assert ids(search(a, "stopping")) == [e["id"]]
    assert ids(search(a, "stopping", scope="transcript")) == [e["id"]]
    assert search(a, "stopping", scope="message")["items"] == []
    assert ids(search(a, "pump", scope="message")) == [e["id"]]
    assert search(a, "alarm", scope="transcript")["items"] == []


def test_transcript_is_searchable_only_once_it_exists():
    a = make_archive()
    add_audio(a)
    assert search(a, "stopping")["items"] == []
    work(a)
    assert len(search(a, "stopping")["items"]) == 1


def test_hostile_queries_are_just_words():
    a = loaded()
    for q in ['pump" OR 1=1', "NEAR(a b)", "message:pump", "pump AND leak", "*", "(", "^pump"]:
        try:
            search(a, q)
        except Invalid:
            continue
    assert len(search(a, "pump*")["items"]) > 0
    assert search(a, 'x" OR "pump')["items"] == []  # AND of x, or, pump: no event has 'x'


def test_snippet_marks_the_match():
    a = make_archive()
    add(a, message="Seal leak found on the pump")
    item = search(a, "leak")["items"][0]
    assert "[[leak]]" in item["snippet"]
    assert "snippet" not in search(a)["items"][0]


# ------------------------------------------------------------------ filters
def test_filters_combine():
    a = loaded()
    r = search(a, asset_id="PMP-0001", kind="alarm", severity="critical")
    assert [(i["asset_id"], i["kind"], i["severity"]) for i in r["items"]] == [("PMP-0001", "alarm", "critical")]
    assert len(search(a, kind="alarm")["items"]) == 9
    assert len(search(a, severity="warning")["items"]) == 6
    assert len(search(a, "temperature", kind="alarm", severity="warning")["items"]) == 2


def test_time_window_is_since_inclusive_until_exclusive():
    a = make_archive()
    for t in ("06:00:00", "06:00:01", "06:00:02"):
        add(a, occurred_at=f"2026-10-12T{t}Z", message=f"at {t}")
    got = lambda **k: [i["occurred_at"][11:] for i in search(a, **k, sort="time")["items"]]  # noqa: E731
    assert got(since="2026-10-12T06:00:01Z") == ["06:00:02Z", "06:00:01Z"]
    assert got(until="2026-10-12T06:00:02Z") == ["06:00:01Z", "06:00:00Z"]
    assert got(since="2026-10-12T06:00:01Z", until="2026-10-12T06:00:02Z") == ["06:00:01Z"]
    assert got(since="2026-10-12T09:30:01+03:30") == ["06:00:02Z", "06:00:01Z"]  # offsets honoured


def test_has_recording_filter():
    a = make_archive()
    plain = add(a, message="no audio")
    voiced = add_audio(a, message="with audio")
    assert ids(search(a, has_recording=True)) == [voiced["id"]]
    assert ids(search(a, has_recording=False)) == [plain["id"]]
    assert set(ids(search(a))) == {plain["id"], voiced["id"]}


@pytest.mark.parametrize("kw", [dict(kind="fault"), dict(severity="high"), dict(since="yesterday"),
                                dict(until="2026-10-12T06:00:00"), dict(scope="meta"), dict(sort="random"),
                                dict(limit=0), dict(limit=101), dict(offset=-1), dict(offset=1001)])  # fmt: skip
def test_bad_parameters_are_rejected(kw):
    a = loaded()
    n = audit_count(a)
    with pytest.raises(Invalid):
        search(a, "pump", **kw)
    assert audit_count(a) == n  # a rejected search is not recorded as a search


def test_relevance_needs_a_query_and_cursor_needs_time_sort():
    a = loaded()
    with pytest.raises(Invalid, match="needs a query"):
        search(a, sort="relevance")
    with pytest.raises(Invalid, match="cursor"):
        search(a, "pump", sort="relevance", cursor="x")
    with pytest.raises(Invalid, match="cursor"):
        search(a, "pump", cursor="eHx4", offset=3)


# ------------------------------------------------------------------ ordering and paging
def test_default_order_is_newest_first_with_id_as_tiebreak():
    a = make_archive()
    e1 = add(a, occurred_at="2026-10-12T06:00:00Z", message="one")
    e2 = add(a, occurred_at="2026-10-12T06:00:00Z", message="two")
    e3 = add(a, occurred_at="2026-10-12T05:00:00Z", message="three")
    e4 = add(a, occurred_at="2026-10-12T07:00:00Z", message="four")
    assert ids(search(a)) == [e4["id"], e2["id"], e1["id"], e3["id"]]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 20, 50])
def test_keyset_paging_visits_every_event_exactly_once(size):
    a = loaded()
    for i in range(5):  # same-second ties across page boundaries
        add(a, occurred_at="2026-10-12T05:00:00Z", message=f"tie {i}")
    seen, cursor, pages = [], None, 0
    while True:
        r = search(a, limit=size, cursor=cursor)
        seen += ids(r)
        pages += 1
        cursor = r["next_cursor"]
        if cursor is None:
            break
        assert pages < 100
    assert len(seen) == len(set(seen)) == 25
    assert seen == ids(search(a, limit=100))


def test_paging_is_stable_when_new_events_arrive():
    a = loaded()
    first = search(a, limit=5)
    add(a, occurred_at="2026-10-12T11:00:00Z", message="newer than everything")
    second = search(a, limit=5, cursor=first["next_cursor"])
    assert not set(ids(first)) & set(ids(second))  # nothing repeated, unlike offset paging


def test_last_page_has_no_cursor():
    a = loaded()
    assert search(a, limit=100)["next_cursor"] is None
    assert search(a, limit=20)["next_cursor"] is None  # exactly 20 events, no extra page
    assert search(a, limit=19)["next_cursor"] is not None


@pytest.mark.parametrize("cursor", ["!!!", "bm9wZQ", "Z2FyYmFnZXw1", "MjAyNi0xMC0xMlQwNjowMDowMFp8eA", "MjAyNi0xMC0xMg"])
def test_bad_cursors_are_rejected(cursor):
    with pytest.raises(Invalid, match="cursor"):
        search(loaded(), cursor=cursor)


def test_relevance_orders_by_score_and_pages_by_offset():
    a = make_archive()
    weak = add(a, message="pump started")
    strong = add(a, message="pump pump pump trip", tags=["pump"])
    for n in range(6):  # bm25 needs a corpus where the term is rare, or every score collapses to 0
        add(a, message=f"unrelated {n}")
    r = search(a, "pump", sort="relevance")
    assert ids(r) == [strong["id"], weak["id"]]
    scores = [i["score"] for i in r["items"]]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[1] > 0
    p1 = search(a, "pump", sort="relevance", limit=1)
    p2 = search(a, "pump", sort="relevance", limit=1, offset=p1["next_offset"])
    assert (ids(p1), ids(p2)) == ([strong["id"]], [weak["id"]]) and p2["next_offset"] is None


# ------------------------------------------------------------------ audit of searches, permissions
def test_every_search_is_audited_without_the_matched_text():
    a = make_archive()
    add(a, message="CONFIDENTIAL-MARKER pump")
    n = audit_count(a)
    search(a, "پمپ  PUMP", severity="critical", scope="message")
    row = a.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["actor"], row["action"]) == (V, "search")
    d = json.loads(row["detail"])
    assert d == {"q": "پمپ pump", "scope": "message", "sort": "time", "filters": {"severity": "critical"}, "returned": 0}
    assert audit_count(a) == n + 1
    assert "CONFIDENTIAL-MARKER" not in row["detail"]


def test_search_audit_counts_results():
    a = loaded()
    search(a, "pump")
    assert json.loads(a.db.execute("SELECT detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0])["returned"] == 4


@pytest.mark.parametrize("who", ["view.a", "op.b", "adm.d"])
def test_readers_may_search(who):
    a = loaded()
    assert a.search(a.P[who], "pump")["items"]


def test_auditor_may_not_read_event_content():
    a = loaded()
    with pytest.raises(Forbidden):
        a.search(a.P["aud.e"], "pump")
    with pytest.raises(Forbidden):
        a.get(a.P["aud.e"], 1)


def test_ties_are_ordered_by_id_also_when_a_text_query_drives_the_plan():
    a = make_archive()
    made = [add(a, occurred_at="2026-10-12T05:00:00Z", message=f"tie word {i}")["id"] for i in range(6)]
    assert ids(search(a, "tie")) == made[::-1]
    seen, cursor = [], None
    while True:
        r = search(a, "tie", limit=2, cursor=cursor)
        seen += ids(r)
        cursor = r["next_cursor"]
        if cursor is None:
            break
    assert seen == made[::-1]


def test_offset_may_reach_the_limit_exactly():
    a = loaded()
    assert search(a, "pump", sort="relevance", offset=1000)["items"] == []
