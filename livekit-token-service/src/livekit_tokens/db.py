"""SQLite schema. Sessions and an append-only audit table (UPDATE/DELETE blocked by triggers)."""

from __future__ import annotations

import hashlib
import sqlite3

SEED_USERS = [  # fictional people; (username, display name, role)
    ("tech.a", "Technician A", "technician"),
    ("tech.b", "Technician B", "technician"),
    ("exp.c", "Expert C", "expert"),
    ("sup.d", "Supervisor D", "supervisor"),
    ("adm.e", "Admin E", "admin"),
]
ROLES = ("technician", "expert", "supervisor", "admin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('technician','expert','supervisor','admin'))
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token_sha256 TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL UNIQUE,
    asset_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(username),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    closed_at INTEGER,
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn
