"""SQLite schema, connection and seeding.

The audit trail is protected *in the database*: triggers abort any UPDATE or DELETE on
``audit_events``, so not even a bug in the application code can rewrite history.
"""

from __future__ import annotations

import hashlib
import sqlite3

SCHEMA = """
CREATE TABLE users (
    username     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('submitter', 'reviewer', 'supervisor')),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE api_tokens (
    token_sha256 TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES users (username),
    created_at   TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);
CREATE TABLE requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL,
    asset_id       TEXT NOT NULL,
    priority       TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    estimated_cost INTEGER NOT NULL CHECK (estimated_cost >= 0),
    status         TEXT NOT NULL CHECK (status IN
                     ('draft', 'submitted', 'reviewed', 'returned', 'approved', 'rejected', 'withdrawn')),
    owner          TEXT NOT NULL REFERENCES users (username),
    reviewed_by    TEXT REFERENCES users (username),
    revision       INTEGER NOT NULL DEFAULT 1,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES requests (id),
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    comment     TEXT
);
CREATE TABLE idempotency_keys (
    username   TEXT NOT NULL REFERENCES users (username),
    key        TEXT NOT NULL,
    body_hash  TEXT NOT NULL,
    request_id INTEGER NOT NULL REFERENCES requests (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (username, key)
);
CREATE INDEX idx_requests_status ON requests (status, id);
CREATE INDEX idx_requests_owner ON requests (owner, id);
CREATE INDEX idx_audit_request ON audit_events (request_id, id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""

# Fictional people. Two supervisors on purpose: high-cost requests need two different people.
SEED_USERS = (
    ("sub.a", "Sample Submitter A", "submitter"),
    ("sub.b", "Sample Submitter B", "submitter"),
    ("rev.c", "Sample Reviewer C", "reviewer"),
    ("rev.d", "Sample Reviewer D", "reviewer"),
    ("sup.e", "Sample Supervisor E", "supervisor"),
    ("sup.f", "Sample Supervisor F", "supervisor"),
)


def hash_token(token: str) -> str:
    """Tokens are stored as SHA-256 only; the plain token is shown once, when it is issued."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open the database, creating the schema and the fictional users when it is new."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'requests'").fetchone()
    if not exists:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO users (username, display_name, role) VALUES (?, ?, ?)", SEED_USERS)
    return conn
