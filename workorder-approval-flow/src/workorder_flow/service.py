"""Application logic. Pure Python + sqlite3; knows nothing about HTTP.

Every write runs in ONE transaction that changes the request AND appends the audit event, so the
two cannot disagree. Concurrent changes are detected with a ``version`` number: a caller who acts on
a stale copy gets ``StaleVersion`` instead of overwriting someone else's decision.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import states as st
from .db import hash_token
from .errors import (
    Forbidden, IdempotencyConflict, Invalid, InvalidTransition, NotFound, StaleVersion, Unauthorized,
)  # fmt: skip
from .models import CreateRequest, Decision, EditRequest, VersionOnly, valid_idempotency_key

Clock = Callable[[], datetime]
DEFAULT_REVIEWER_LIMIT = 50_000_000  # rials: above this a supervisor must give the final approval
MAX_LIMIT = 100


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Principal:
    username: str
    display_name: str
    role: str


class Flow:
    def __init__(self, conn: sqlite3.Connection, clock: Clock = utc_now,
                 reviewer_limit: int = DEFAULT_REVIEWER_LIMIT) -> None:  # fmt: skip
        self.db = conn
        self.clock = clock
        self.reviewer_limit = reviewer_limit
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return _stamp(self.clock())

    def _tx(self):
        return _Tx(self.db)

    def _row(self, request_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFound(f"request {request_id} does not exist")
        return row

    @staticmethod
    def _visible(p: Principal, row: sqlite3.Row) -> bool:
        """Drafts are private to their owner. Everything else is visible to reviewers and
        supervisors; submitters see only their own requests."""
        if row["owner"] == p.username:
            return True
        if row["status"] == st.DRAFT:
            return False
        return p.role in (st.REVIEWER, st.SUPERVISOR)

    def _visible_row(self, p: Principal, request_id: int) -> sqlite3.Row:
        row = self._row(request_id)
        if not self._visible(p, row):
            raise NotFound(f"request {request_id} does not exist")  # same answer as a missing one
        return row

    def _owned_row(self, p: Principal, request_id: int) -> sqlite3.Row:
        row = self._visible_row(p, request_id)
        if row["owner"] != p.username:
            raise Forbidden("only the person who created the request can do this")
        return row

    @staticmethod
    def _check_version(row: sqlite3.Row, version: int) -> None:
        if row["version"] != version:
            raise StaleVersion(
                f"the request changed since you read it (you have version {version}, "
                f"it is now version {row['version']}); fetch it again and retry",
                current_version=row["version"],
            )

    def _write(self, row: sqlite3.Row, p: Principal, action: str, *, to_status: str | None = None,
               comment: str | None = None, fields: dict | None = None, revision: int | None = None,
               reviewed_by: object = ...) -> dict:  # fmt: skip
        """Apply a change and append the audit event. Must be called inside a transaction."""
        new_status = to_status or row["status"]
        new_version = row["version"] + 1
        new_revision = revision if revision is not None else row["revision"]
        sets = {"status": new_status, "version": new_version, "revision": new_revision,
                "updated_at": self._now(), **(fields or {})}  # fmt: skip
        if reviewed_by is not ...:
            sets["reviewed_by"] = reviewed_by
        cols = ", ".join(f"{k} = ?" for k in sets)
        cur = self.db.execute(f"UPDATE requests SET {cols} WHERE id = ? AND version = ?",
                              [*sets.values(), row["id"], row["version"]])  # fmt: skip
        if cur.rowcount != 1:  # lost a race despite the lock (another process): same answer as stale
            raise StaleVersion("the request was changed by someone else at the same moment; retry")
        self.db.execute(
            "INSERT INTO audit_events (request_id, at, actor, action, from_status, to_status, revision, "
            "version, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], self._now(), p.username, action, row["status"], new_status, new_revision,
             new_version, comment),
        )  # fmt: skip
        return self._view(self._row(row["id"]))

    @staticmethod
    def _view(row: sqlite3.Row) -> dict:
        return dict(row)

    def _decorate(self, d: dict) -> dict:
        d["needs_supervisor"] = d["estimated_cost"] > self.reviewer_limit
        d["next_step"] = self._next_step(d)
        return d

    def _next_step(self, d: dict) -> str:
        s = d["status"]
        if s == st.DRAFT:
            return "owner: submit"
        if s == st.RETURNED:
            return "owner: edit and submit again"
        if s == st.SUBMITTED:
            return "reviewer or supervisor: decide"
        if s == st.REVIEWED:
            return "a different supervisor: final decision"
        return "none (final)"

    # ------------------------------------------------------------------ authentication
    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("missing bearer token")
        row = self.db.execute(
            "SELECT u.username, u.display_name, u.role FROM api_tokens t JOIN users u ON u.username = t.username "
            "WHERE t.token_sha256 = ? AND t.revoked = 0 AND u.active = 1", (hash_token(token),),
        ).fetchone()  # fmt: skip
        if row is None:
            raise Unauthorized("invalid or revoked token")
        return Principal(row["username"], row["display_name"], row["role"])

    def issue_token(self, username: str) -> str:
        """Create a new random token for ``username``. The plain token is returned once; only its
        SHA-256 is stored."""
        with self._lock, self._tx():
            user = self.db.execute("SELECT username FROM users WHERE username = ? AND active = 1",
                                   (username,)).fetchone()  # fmt: skip
            if user is None:
                raise NotFound(f"unknown or inactive user {username!r}")
            token = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                            (hash_token(token), username, self._now()))  # fmt: skip
            return token

    # ------------------------------------------------------------------ reads
    def get(self, p: Principal, request_id: int) -> dict:
        with self._lock:
            return self._decorate(self._view(self._visible_row(p, request_id)))

    def history(self, p: Principal, request_id: int) -> list[dict]:
        with self._lock:
            self._visible_row(p, request_id)
            rows = self.db.execute(
                "SELECT at, actor, action, from_status, to_status, revision, version, comment "
                "FROM audit_events WHERE request_id = ? ORDER BY id", (request_id,),
            ).fetchall()  # fmt: skip
            return [dict(r) for r in rows]

    def _page(self, where: list[str], args: list, limit: int, offset: int) -> dict:
        if not 1 <= limit <= MAX_LIMIT:
            raise Invalid(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise Invalid("offset must be >= 0")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM requests {clause}", args).fetchone()[0]
        rows = self.db.execute(f"SELECT * FROM requests {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                               [*args, limit, offset]).fetchall()  # fmt: skip
        items = [self._decorate(self._view(r)) for r in rows]
        return {"items": items, "total": total,
                "next_offset": offset + len(items) if offset + len(items) < total else None}  # fmt: skip

    def _visibility_sql(self, p: Principal) -> tuple[str, list]:
        if p.role in (st.REVIEWER, st.SUPERVISOR):
            return "(owner = ? OR status != 'draft')", [p.username]
        return "owner = ?", [p.username]

    def list_requests(self, p: Principal, *, status: str | None = None, mine: bool = False,
                      limit: int = 20, offset: int = 0) -> dict:  # fmt: skip
        if status is not None and status not in st.STATUSES:
            raise Invalid(f"status must be one of {', '.join(st.STATUSES)}")
        sql, args = self._visibility_sql(p)
        where, params = [sql], list(args)
        if status:
            where.append("status = ?")
            params.append(status)
        if mine:
            where.append("owner = ?")
            params.append(p.username)
        with self._lock:
            return self._page(where, params, limit, offset)

    def queue(self, p: Principal, *, limit: int = 20, offset: int = 0) -> dict:
        """What this person can act on RIGHT NOW: requests they may decide (role and four-eyes rules
        already applied) plus their own requests that wait for them (draft / returned)."""
        parts: list[str] = []
        args: list = []
        if p.role in (st.REVIEWER, st.SUPERVISOR):
            parts.append("(status = 'submitted' AND owner != ?)")
            args.append(p.username)
        if p.role == st.SUPERVISOR:
            parts.append("(status = 'reviewed' AND owner != ? AND (reviewed_by IS NULL OR reviewed_by != ?))")
            args += [p.username, p.username]
        parts.append("(owner = ? AND status IN ('draft', 'returned'))")
        args.append(p.username)
        with self._lock:
            return self._page(["(" + " OR ".join(parts) + ")"], args, limit, offset)

    # ------------------------------------------------------------------ writes
    def create(self, p: Principal, body: CreateRequest, idempotency_key: str | None = None) -> tuple[dict, bool]:
        """Returns ``(request, created)``. With an idempotency key, repeating the same call returns
        the original request (``created=False``); the same key with a different body is refused."""
        if idempotency_key is not None and not valid_idempotency_key(idempotency_key):
            raise Invalid("Idempotency-Key must be 8-64 characters of letters, digits and . _ : -")
        body_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        with self._lock, self._tx():
            if idempotency_key:
                prev = self.db.execute(
                    "SELECT body_hash, request_id FROM idempotency_keys WHERE username = ? AND key = ?",
                    (p.username, idempotency_key),
                ).fetchone()  # fmt: skip
                if prev:
                    if prev["body_hash"] != body_hash:
                        raise IdempotencyConflict("this Idempotency-Key was already used with a different request body")
                    return self._decorate(self._view(self._row(prev["request_id"]))), False
            now = self._now()
            cur = self.db.execute(
                "INSERT INTO requests (title, description, asset_id, priority, estimated_cost, status, owner, "
                "revision, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, 1, 1, ?, ?)",
                (body.title, body.description, body.asset_id, body.priority, body.estimated_cost, p.username,
                 now, now),
            )  # fmt: skip
            rid = cur.lastrowid
            self.db.execute(
                "INSERT INTO audit_events (request_id, at, actor, action, from_status, to_status, revision, version, "
                "comment) VALUES (?, ?, ?, 'created', NULL, 'draft', 1, 1, NULL)", (rid, now, p.username),
            )  # fmt: skip
            if idempotency_key:
                self.db.execute("INSERT INTO idempotency_keys (username, key, body_hash, request_id, created_at) "
                                "VALUES (?, ?, ?, ?, ?)", (p.username, idempotency_key, body_hash, rid, now))  # fmt: skip
            return self._decorate(self._view(self._row(rid))), True

    def edit(self, p: Principal, request_id: int, body: EditRequest) -> dict:
        with self._lock, self._tx():
            row = self._owned_row(p, request_id)
            self._check_version(row, body.version)
            if row["status"] not in st.EDITABLE:
                raise InvalidTransition(
                    f"a request in status '{row['status']}' cannot be edited (only draft or returned)")
            changes = {k: v for k, v in body.model_dump(exclude={"version"}).items() if v is not None}
            if not changes:
                raise Invalid("nothing to change: send at least one field to edit")
            return self._decorate(self._write(row, p, "edited", fields=changes))

    def submit(self, p: Principal, request_id: int, body: VersionOnly) -> dict:
        with self._lock, self._tx():
            row = self._owned_row(p, request_id)
            self._check_version(row, body.version)
            if row["status"] not in st.EDITABLE:
                raise InvalidTransition(f"a request in status '{row['status']}' cannot be submitted")
            resubmit = row["status"] == st.RETURNED
            return self._decorate(self._write(
                row, p, "resubmitted" if resubmit else "submitted", to_status=st.SUBMITTED, comment=body.comment,
                revision=row["revision"] + 1 if resubmit else row["revision"], reviewed_by=None))  # fmt: skip

    def withdraw(self, p: Principal, request_id: int, body: VersionOnly) -> dict:
        with self._lock, self._tx():
            row = self._owned_row(p, request_id)
            self._check_version(row, body.version)
            if row["status"] not in st.WITHDRAWABLE:
                raise InvalidTransition(f"a request in status '{row['status']}' cannot be withdrawn")
            return self._decorate(self._write(row, p, "withdrawn", to_status=st.WITHDRAWN, comment=body.comment))

    def decide(self, p: Principal, request_id: int, body: Decision) -> dict:
        with self._lock, self._tx():
            row = self._visible_row(p, request_id)
            # Order matters: role and four-eyes first (so someone with no right to decide learns nothing
            # about the request's state), then 'is your copy current', then 'is this step possible'.
            st.check_decider(role=p.role, status=row["status"], actor=p.username, owner=row["owner"],
                             reviewed_by=row["reviewed_by"])  # fmt: skip
            self._check_version(row, body.version)
            outcome = st.decide(row["status"], body.action, row["estimated_cost"], self.reviewer_limit)
            if body.action in (st.REJECT, st.RETURN) and not body.comment:
                raise Invalid(f"a comment of at least 5 characters is required to {body.action} a request")
            reviewed_by: object = ...
            if outcome.set_reviewed_by:
                reviewed_by = p.username
            elif outcome.clear_reviewed_by:
                reviewed_by = None
            action = {"approve": "approved" if outcome.status == st.APPROVED else "reviewed",
                      "reject": "rejected", "return": "returned"}[body.action]  # fmt: skip
            return self._decorate(self._write(row, p, action, to_status=outcome.status, comment=body.comment,
                                              reviewed_by=reviewed_by))  # fmt: skip


class _Tx:
    """All-or-nothing writes. Nested use joins the outer transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self):
        self.outer = self.db.in_transaction
        if not self.outer:
            self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb):
        if not self.outer:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
