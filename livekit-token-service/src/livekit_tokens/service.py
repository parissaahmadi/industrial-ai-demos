"""All rules live here: who may open a session, who may join it and as what, how long a token lives."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import db
from .errors import (
    Forbidden,
    Invalid,
    NotFound,
    RateLimited,
    SessionClosed,
    SessionExists,
    SessionExpired,
    Unauthorized,
)
from .grants import grant_for
from .ratelimit import SlidingWindow
from .settings import Settings
from .token import mint

ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Principal:
    username: str
    display_name: str
    role: str


class Service:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, now: Callable[[], float],
                 *, room_suffix: Callable[[], str] | None = None) -> None:  # fmt: skip
        self.db, self.settings, self._now = conn, settings, now
        self._room_suffix = room_suffix or (lambda: secrets.token_hex(8))
        self._lock = threading.RLock()
        self._limiter = SlidingWindow(settings.rate_per_minute, 60)

    def now(self) -> int:
        return int(self._now())

    # ------------------------------------------------------------------ users
    def seed_users(self) -> None:
        with self._lock:
            for username, display, role in db.SEED_USERS:
                self.db.execute("INSERT OR IGNORE INTO users (username, display_name, role) VALUES (?, ?, ?)",
                                (username, display, role))  # fmt: skip

    def issue_api_token(self, username: str) -> str:
        """A long random bearer token for the API (not a LiveKit token). Only its SHA-256 is stored."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            if not self.db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise NotFound(f"no such user {username!r}")
            self.db.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                            (db.hash_token(token), username, self.now()))  # fmt: skip
        return token

    def authenticate(self, token: str) -> Principal:
        row = self.db.execute(
            "SELECT u.username, u.display_name, u.role FROM api_tokens t JOIN users u USING (username) "
            "WHERE t.token_sha256 = ?", (db.hash_token(token),)).fetchone()  # fmt: skip
        if not row:
            raise Unauthorized("unknown or revoked API token")
        return Principal(row["username"], row["display_name"], row["role"])

    # ------------------------------------------------------------------ audit
    def _audit(self, actor: str, action: str, target: str, **detail) -> None:
        self.db.execute("INSERT INTO audit (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                        (self.now(), actor, action, target, json.dumps(detail, sort_keys=True)))  # fmt: skip

    def _deny(self, p: Principal, action: str, target: str, why: str) -> Forbidden:
        self._audit(p.username, "access.denied", target, attempted=action, why=why)
        return Forbidden(why)

    def audit_entries(self, p: Principal, *, after_id: int = 0, limit: int = 50) -> dict:
        if p.role != "admin":
            raise self._deny(p, "audit.read", "audit", f"the role {p.role!r} may not read the audit trail")
        if not 1 <= limit <= 200:
            raise Invalid("limit must be 1-200")
        rows = self.db.execute("SELECT * FROM audit WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return {"items": [{"id": r["id"], "at": iso(r["ts"]), "actor": r["actor"], "action": r["action"],
                           "target": r["target"], "detail": json.loads(r["detail"])} for r in rows]}  # fmt: skip

    # ------------------------------------------------------------------ sessions
    def _view(self, r: sqlite3.Row) -> dict:
        now = self.now()
        state = "closed" if r["closed_at"] is not None else "expired" if now >= r["expires_at"] else "open"
        return {"id": r["id"], "room": r["room"], "asset_id": r["asset_id"], "reason": r["reason"],
                "state": state, "created_by": r["created_by"], "created_at": iso(r["created_at"]),
                "expires_at": iso(r["expires_at"]), "closed_at": iso(r["closed_at"]),
                "closed_by": r["closed_by"]}  # fmt: skip

    def _row(self, session_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise NotFound(f"no session {session_id}")
        return row

    def create_session(self, p: Principal, asset_id: str, reason: str) -> dict:
        if p.role != "technician":
            raise self._deny(p, "session.create", "sessions", "only a technician can ask for remote help")
        asset_id, reason = asset_id.strip(), reason.strip()
        if not ASSET_RE.fullmatch(asset_id):
            raise Invalid("asset_id must be 1-40 letters, digits, '.', '_' or '-'")
        if not 1 <= len(reason) <= 200 or _CONTROL.search(reason):
            raise Invalid("reason must be 1-200 characters without control characters")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                busy = self.db.execute(
                    "SELECT id FROM sessions WHERE created_by = ? AND closed_at IS NULL AND expires_at > ?",
                    (p.username, now)).fetchone()  # fmt: skip
                if busy:
                    raise SessionExists(f"you already have an open session ({busy['id']}); close it first",
                                        session_id=busy["id"])  # fmt: skip
                room = f"assist-{self._room_suffix()}"
                cur = self.db.execute(
                    "INSERT INTO sessions (room, asset_id, reason, created_by, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (room, asset_id, reason, p.username, now, now + self.settings.session_seconds))  # fmt: skip
                self._audit(p.username, "session.create", f"session:{cur.lastrowid}", asset=asset_id)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return self._view(self._row(cur.lastrowid))

    def _visible(self, p: Principal, row: sqlite3.Row) -> bool:
        return p.role != "technician" or row["created_by"] == p.username

    def get_session(self, p: Principal, session_id: int) -> dict:
        row = self._row(session_id)
        if not self._visible(p, row):
            raise self._deny(p, "session.read", f"session:{session_id}", "this is another technician's session")
        return self._view(row)

    def list_sessions(self, p: Principal) -> dict:
        if p.role == "technician":
            rows = self.db.execute("SELECT * FROM sessions WHERE created_by = ? ORDER BY id DESC LIMIT 20",
                                   (p.username,)).fetchall()  # fmt: skip
        else:
            rows = self.db.execute("SELECT * FROM sessions WHERE closed_at IS NULL AND expires_at > ? "
                                   "ORDER BY id DESC LIMIT 100", (self.now(),)).fetchall()  # fmt: skip
        return {"items": [self._view(r) for r in rows]}

    def close_session(self, p: Principal, session_id: int) -> dict:
        with self._lock:
            row = self._row(session_id)
            if p.role != "admin" and row["created_by"] != p.username:
                raise self._deny(p, "session.close", f"session:{session_id}", "only its owner or an admin can close it")
            if row["closed_at"] is None:
                self.db.execute("UPDATE sessions SET closed_at = ?, closed_by = ? WHERE id = ? AND closed_at IS NULL",
                                (self.now(), p.username, session_id))  # fmt: skip
                self._audit(p.username, "session.close", f"session:{session_id}")
            return self._view(self._row(session_id))

    # ------------------------------------------------------------------ tokens
    @staticmethod
    def participant_role(p: Principal, row: sqlite3.Row) -> str | None:
        if p.role == "technician":
            return "technician" if row["created_by"] == p.username else None
        return {"expert": "expert", "supervisor": "observer", "admin": "observer"}.get(p.role)

    def issue_token(self, p: Principal, session_id: int, ttl: int | None = None) -> dict:
        s = self.settings
        with self._lock:
            row = self._row(session_id)
            target = f"session:{session_id}"
            part = self.participant_role(p, row)
            if part is None:
                raise self._deny(p, "token.issue", target, "this is another technician's session")
            view = self._view(row)
            if view["state"] == "closed":
                self._audit(p.username, "token.refused", target, why="closed")
                raise SessionClosed("the session was closed; open a new one", session_id=session_id)
            if view["state"] == "expired":
                self._audit(p.username, "token.refused", target, why="expired")
                raise SessionExpired("the session has run out; open a new one", session_id=session_id)
            if ttl is None:
                ttl = s.default_ttl
            if isinstance(ttl, bool) or not isinstance(ttl, int) or not s.min_ttl <= ttl <= s.max_ttl:
                raise Invalid(f"ttl_seconds must be an integer from {s.min_ttl} to {s.max_ttl}")
            try:
                self._limiter.hit(p.username, self._now())
            except RateLimited:
                self._audit(p.username, "token.rate_limited", target)
                raise
            now = self.now()
            exp = min(now + ttl, row["expires_at"])  # a token never outlives its session
            token, claims = mint(
                s, identity=p.username, name=p.display_name, grant=grant_for(part, row["room"]), now=now, exp=exp,
                metadata=json.dumps({"role": part, "asset": row["asset_id"]}, separators=(",", ":")),
                attributes={"role": part})  # fmt: skip
            fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]  # the token itself is never stored
            self._audit(p.username, "token.issue", target, role=part, exp=exp, fingerprint=fingerprint)
            return {"token": token, "url": s.url, "room": row["room"], "identity": p.username, "role": part,
                    "expires_at": iso(exp), "expires_in": exp - now}  # fmt: skip
