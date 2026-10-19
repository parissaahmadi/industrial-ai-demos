import sqlite3

import pytest

from event_archive import audit
from event_archive.db import connect


def make(n=5):
    db = connect()
    db.execute("BEGIN IMMEDIATE")
    for i in range(n):
        audit.append(db, f"2026-10-12T08:00:0{i}Z", "op.b", "operator", "event.ingest", f"event:{i}", {"n": i})
    db.execute("COMMIT")
    return db


def tamper(db, sql, *args):
    db.execute("DROP TRIGGER IF EXISTS audit_no_update")
    db.execute("DROP TRIGGER IF EXISTS audit_no_delete")
    db.execute(sql, args)


def test_empty_chain_is_valid():
    r = audit.verify(connect())
    assert r == {"ok": True, "count": 0, "head": audit.GENESIS, "broken_at": None, "reason": None}


def test_chain_verifies_and_links_rows():
    db = make()
    r = audit.verify(db)
    assert r["ok"] and r["count"] == 5
    rows = db.execute("SELECT * FROM audit ORDER BY id").fetchall()
    assert [x["id"] for x in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["prev_hash"] == audit.GENESIS
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, 5))
    assert r["head"] == rows[-1]["hash"]


def test_hash_depends_on_previous_hash_and_on_every_field():
    row = {"id": 1, "at": "t", "actor": "a", "role": "r", "action": "x", "target": "y", "detail": "{}"}
    base = audit.row_hash(audit.GENESIS, row)
    assert audit.row_hash("1" * 64, row) != base
    for field in row:
        changed = {**row, field: 2 if field == "id" else "z"}
        assert audit.row_hash(audit.GENESIS, changed) != base, field


def test_detail_is_canonical():
    assert audit.canonical({"b": 1, "a": 2}) == audit.canonical({"a": 2, "b": 1}) == '{"a":2,"b":1}'
    assert audit.canonical({"t": "نشت"}) == '{"t":"نشت"}'  # not escaped


def test_database_refuses_update_and_delete():
    db = make()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit SET actor = 'x' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit WHERE id = 1")
    assert audit.verify(db)["ok"]


@pytest.mark.parametrize("column", ["at", "actor", "role", "action", "target", "detail"])
def test_editing_any_field_is_detected_at_that_row(column):
    db = make()
    tamper(db, f"UPDATE audit SET {column} = 'tampered' WHERE id = 3")
    r = audit.verify(db)
    assert (r["ok"], r["broken_at"]) == (False, 3)
    assert "hash" in r["reason"]


def test_deleting_a_middle_row_is_detected():
    db = make()
    tamper(db, "DELETE FROM audit WHERE id = 3")
    r = audit.verify(db)
    assert (r["ok"], r["broken_at"]) == (False, 4) and "missing" in r["reason"]


def test_rewriting_hash_of_one_row_breaks_the_next_link():
    db = make()
    tamper(db, "UPDATE audit SET hash = ? WHERE id = 2", "f" * 64)
    r = audit.verify(db)
    assert (r["ok"], r["broken_at"]) == (False, 2)


def test_inserting_a_forged_row_is_detected():
    db = make()
    db.execute("DROP TRIGGER audit_no_delete")
    db.execute("DELETE FROM audit WHERE id = 5")
    db.execute("INSERT INTO audit VALUES (5, 't', 'evil', 'admin', 'x', 'y', '{}', ?, ?)", ("0" * 64, "1" * 64))
    r = audit.verify(db)
    assert (r["ok"], r["broken_at"]) == (False, 5)


def test_removed_tail_is_only_caught_with_a_saved_head():
    db = make()
    head = audit.verify(db)["head"]
    tamper(db, "DELETE FROM audit WHERE id > 3")
    assert audit.verify(db)["ok"]  # a shorter chain is internally consistent: the documented limit
    r = audit.verify(db, expected_head=head)
    assert r["ok"] is False and r["count"] == 3 and "head" in r["reason"]


def test_matching_expected_head_passes():
    db = make()
    assert audit.verify(db, expected_head=audit.verify(db)["head"])["ok"]


def test_editing_only_the_prev_hash_column_is_detected():
    db = make()
    tamper(db, "UPDATE audit SET prev_hash = ? WHERE id = 3", "a" * 64)
    r = audit.verify(db)
    assert (r["ok"], r["broken_at"]) == (False, 3) and "prev_hash" in r["reason"]
