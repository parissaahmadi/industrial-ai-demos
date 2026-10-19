"""Hash-chained audit trail.

Each row stores ``hash = SHA-256(prev_hash + "\\n" + canonical_json(row fields))`` and its id is
the previous id + 1. Editing or deleting a row, inserting one, or reordering breaks the chain at a
known id. Two honest limits (see README): someone who can rewrite the *whole* table can recompute
every hash, and removing the *tail* leaves a valid shorter chain. Both are caught only by keeping the
head hash somewhere else; ``verify(expected_head=...)`` supports that.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

GENESIS = "0" * 64
FIELDS = ("id", "at", "actor", "role", "action", "target", "detail")


def canonical(detail: dict) -> str:
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_hash(prev_hash: str, row: dict) -> str:
    payload = json.dumps([row[f] for f in FIELDS], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{prev_hash}\n{payload}".encode()).hexdigest()


def append(db: sqlite3.Connection, at: str, actor: str, role: str, action: str, target: str, detail: dict) -> int:
    """Append one entry. Must be called inside the caller's write transaction."""
    last = db.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    new_id, prev = (last["id"] + 1, last["hash"]) if last else (1, GENESIS)
    row = {"id": new_id, "at": at, "actor": actor, "role": role, "action": action, "target": target,
           "detail": canonical(detail)}  # fmt: skip
    db.execute(
        "INSERT INTO audit (id, at, actor, role, action, target, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (*[row[f] for f in FIELDS], prev, row_hash(prev, row)),
    )
    return new_id


def verify(db: sqlite3.Connection, expected_head: str | None = None) -> dict:
    prev, count, expected_id = GENESIS, 0, 1
    for r in db.execute("SELECT * FROM audit ORDER BY id"):
        row = {f: r[f] for f in FIELDS}
        if r["id"] != expected_id:
            return _bad(count, r["id"], f"expected id {expected_id} (a row is missing or inserted)")
        if r["prev_hash"] != prev:
            return _bad(count, r["id"], "prev_hash does not match the previous row")
        if r["hash"] != row_hash(prev, row):
            return _bad(count, r["id"], "row content does not match its hash")
        prev, count, expected_id = r["hash"], count + 1, expected_id + 1
    if expected_head is not None and expected_head != prev:
        return {"ok": False, "count": count, "head": prev, "broken_at": None,
                "reason": "head hash differs from the expected one (rows were removed from the end or the chain was rewritten)"}  # fmt: skip
    return {"ok": True, "count": count, "head": prev, "broken_at": None, "reason": None}


def _bad(count: int, at: int, reason: str) -> dict:
    return {"ok": False, "count": count, "head": None, "broken_at": at, "reason": reason}
