"""SQLite schema and connection.

* The audit trail is protected *in the database*: triggers abort UPDATE and DELETE on ``audit``.
* ``search`` is an FTS5 table whose rowid is the event id. Text is normalised in Python
  (``textnorm``) before it goes in, so the tokenizer sees one canonical spelling.
"""

from __future__ import annotations

import hashlib
import sqlite3

SCHEMA = """
CREATE TABLE users (
    username     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin', 'auditor')),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE api_tokens (
    token_sha256 TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES users (username),
    created_at   TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    asset_id    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('alarm', 'event', 'note')),
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL,
    source_ref  TEXT,
    body_sha256 TEXT NOT NULL,
    redacted    INTEGER NOT NULL DEFAULT 0 CHECK (redacted IN (0, 1)),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_events_source_ref ON events (source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX idx_events_time ON events (occurred_at DESC, id DESC);
CREATE INDEX idx_events_asset ON events (asset_id, occurred_at DESC, id DESC);
CREATE TABLE recordings (
    event_id           INTEGER PRIMARY KEY REFERENCES events (id),
    sha256             TEXT NOT NULL,
    size               INTEGER NOT NULL,
    audio              BLOB,
    status             TEXT NOT NULL CHECK (status IN
                         ('pending', 'running', 'done', 'failed', 'dead', 'redacted')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    TEXT NOT NULL,
    lease_until        TEXT,
    last_error         TEXT,
    language           TEXT,
    transcript         TEXT,
    transcript_sha256  TEXT,
    transcript_original TEXT,
    confidence         REAL,
    needs_review       INTEGER NOT NULL DEFAULT 0,
    corrected_by       TEXT,
    version            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX idx_recordings_queue ON recordings (status, next_attempt_at);
CREATE VIRTUAL TABLE search USING fts5 (message, transcript, meta, tokenize = 'unicode61');
CREATE TABLE audit (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    role      TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    detail    TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE INDEX idx_audit_action ON audit (action, id);
CREATE INDEX idx_audit_actor ON audit (actor, id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""

# Fictional people, one per role.
SEED_USERS = (
    ("view.a", "Sample Viewer A", "viewer"),
    ("op.b", "Sample Operator B", "operator"),
    ("op.c", "Sample Operator C", "operator"),
    ("adm.d", "Sample Admin D", "admin"),
    ("aud.e", "Sample Auditor E", "auditor"),
)


def hash_token(token: str) -> str:
    """Tokens are stored as SHA-256 only; the plain token is shown once, when it is issued."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open the database, creating the schema and the fictional users when it is new."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone()
    if not exists:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO users (username, display_name, role) VALUES (?, ?, ?)", SEED_USERS)
    return conn
