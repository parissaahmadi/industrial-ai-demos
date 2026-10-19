"""The least-privilege "role" a migration runs under.

SQLite has no users or GRANTs, so the role is an allow-list enforced with
``sqlite3.Connection.set_authorizer`` while a migration's SQL runs. Anything not listed is
denied. The migrator's own bookkeeping runs with the authorizer switched off.

========================  ==============================================================
always allowed            CREATE TABLE / INDEX / VIEW / TRIGGER, ALTER TABLE, INSERT,
                          UPDATE, SELECT, REINDEX, ANALYZE, temp objects
needs ``allow-destructive``  DROP TABLE / INDEX / VIEW / TRIGGER, DELETE
never allowed             ATTACH / DETACH, PRAGMA (except the read-only integrity checks), BEGIN / COMMIT / ROLLBACK / SAVEPOINT,
                          virtual tables, ``load_extension``, and *any* change to
                          ``schema_migrations`` (so a migration cannot forge history)
========================  ==============================================================

Known limit: the authorizer sees *which* statement type runs, not its ``WHERE`` clause,
so ``UPDATE`` is allowed even though it can rewrite every row, and ``ALTER TABLE ... DROP
COLUMN`` is classed as ``ALTER`` (not destructive).
"""

from __future__ import annotations

import sqlite3

BOOKKEEPING_TABLE = "schema_migrations"

_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
_S = sqlite3

ALWAYS = {
    _S.SQLITE_CREATE_TABLE, _S.SQLITE_CREATE_INDEX, _S.SQLITE_CREATE_VIEW, _S.SQLITE_CREATE_TRIGGER,
    _S.SQLITE_CREATE_TEMP_TABLE, _S.SQLITE_CREATE_TEMP_INDEX, _S.SQLITE_CREATE_TEMP_VIEW,
    _S.SQLITE_CREATE_TEMP_TRIGGER, _S.SQLITE_DROP_TEMP_TABLE, _S.SQLITE_DROP_TEMP_INDEX,
    _S.SQLITE_DROP_TEMP_VIEW, _S.SQLITE_DROP_TEMP_TRIGGER, _S.SQLITE_ALTER_TABLE,
    _S.SQLITE_INSERT, _S.SQLITE_UPDATE, _S.SQLITE_SELECT, _S.SQLITE_READ, _S.SQLITE_REINDEX,
    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE,
}  # fmt: skip
DESTRUCTIVE = {
    _S.SQLITE_DROP_TABLE, _S.SQLITE_DROP_INDEX, _S.SQLITE_DROP_VIEW, _S.SQLITE_DROP_TRIGGER,
    _S.SQLITE_DELETE,
}  # fmt: skip
# DDL that names a table in arg2 (for CREATE INDEX / TRIGGER and ALTER) or arg1 (DROP TABLE)
_TOUCHES_TABLE_ARG2 = {_S.SQLITE_CREATE_INDEX, _S.SQLITE_CREATE_TRIGGER, _S.SQLITE_ALTER_TABLE,
                       _S.SQLITE_DROP_INDEX, _S.SQLITE_DROP_TRIGGER}  # fmt: skip
_WRITES = {_S.SQLITE_INSERT, _S.SQLITE_UPDATE, _S.SQLITE_DELETE, _S.SQLITE_DROP_TABLE,
           _S.SQLITE_CREATE_TABLE}  # fmt: skip
_INTERNAL_TABLES = {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
_DENIED_FUNCTIONS = {"load_extension"}
# SQLite itself runs `PRAGMA quick_check` while compiling ``ALTER TABLE ... ADD COLUMN`` with a CHECK
# constraint, so the read-only integrity pragmas must pass. Every other PRAGMA is denied.
_READONLY_PRAGMAS = {"quick_check", "integrity_check", "foreign_key_check"}


class Authorizer:
    """Callable for ``set_authorizer``; remembers why it said no."""

    def __init__(self, allow_destructive: bool = False):
        self.allow_destructive = allow_destructive
        self.reason: str | None = None

    def __call__(self, action, arg1, arg2, dbname, source):
        verdict = self._decide(action, arg1, arg2, dbname)
        if verdict is not None:
            if self.reason is None:
                self.reason = verdict
            return _DENY
        return _OK

    def _decide(self, action, arg1, arg2, dbname=None) -> str | None:
        """Return a human-readable denial reason, or ``None`` to allow."""
        if action == _S.SQLITE_FUNCTION:
            if (arg2 or "").lower() in _DENIED_FUNCTIONS:
                return f"function {arg2}() is not allowed"
            return None
        if action == _S.SQLITE_PRAGMA and (arg1 or "").lower() in _READONLY_PRAGMAS:
            return None
        # The history table is read-only for migrations (reads are fine).
        protected = BOOKKEEPING_TABLE
        if action in _WRITES and arg1 == protected:
            return f"migrations may not modify {protected}"
        if action in _TOUCHES_TABLE_ARG2 and arg2 == protected:
            return f"migrations may not change the structure of {protected}"
        if action in (_S.SQLITE_INSERT, _S.SQLITE_UPDATE, _S.SQLITE_DELETE) and arg1 in _INTERNAL_TABLES:
            return None  # bookkeeping SQLite does itself while running DDL; DDL is gated on its own
        if action in ALWAYS:
            return None
        if action in DESTRUCTIVE:
            if self.allow_destructive or dbname == "temp":  # scratch tables are not data
                return None
            return (
                f"{_ACTION_NAMES.get(action, 'a destructive statement')} is destructive; "
                "add '-- migrate: allow-destructive' at the top of the file if that is intended"
            )
        return f"{_ACTION_NAMES.get(action, f'action {action}')} is not allowed in a migration"


_ACTION_NAMES = {
    _S.SQLITE_DROP_TABLE: "DROP TABLE", _S.SQLITE_DROP_INDEX: "DROP INDEX",
    _S.SQLITE_DROP_VIEW: "DROP VIEW", _S.SQLITE_DROP_TRIGGER: "DROP TRIGGER",
    _S.SQLITE_DELETE: "DELETE", _S.SQLITE_ATTACH: "ATTACH", _S.SQLITE_DETACH: "DETACH",
    _S.SQLITE_PRAGMA: "PRAGMA", _S.SQLITE_TRANSACTION: "transaction control (BEGIN/COMMIT/ROLLBACK)",
    _S.SQLITE_SAVEPOINT: "SAVEPOINT", _S.SQLITE_CREATE_VTABLE: "CREATE VIRTUAL TABLE",
    _S.SQLITE_DROP_VTABLE: "DROP VIRTUAL TABLE",
}  # fmt: skip
