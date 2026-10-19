"""The migration runner.

Guarantees, each covered by a test:

* **Forward only.** There is no "down". A mistake is fixed by a *new* migration.
* **History is tamper-evident.** Applied files may not change (SHA-256), disappear, be renamed,
  or be joined by an older-numbered file that was never applied (out of order).
* **One transaction per migration.** The SQL and its ``schema_migrations`` row commit together,
  or neither does. SQLite DDL is transactional, so a failed migration leaves no half-built schema.
* **Concurrent runners are safe.** Every step starts with ``BEGIN IMMEDIATE`` and re-reads the
  history inside the lock, so two processes can never both apply the same migration.
* **Least privilege.** Migration SQL runs under ``policy.Authorizer``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import IntegrityProblem, MigrationError, MigrationFailed, PolicyViolation
from .files import Migration, load_migrations
from .policy import BOOKKEEPING_TABLE, Authorizer
from .sqlsplit import split_statements

BOOKKEEPING_SQL = f"""
CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} (
    version     INTEGER PRIMARY KEY CHECK (version >= 1),
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL CHECK (length(checksum) = 64),
    applied_at  TEXT    NOT NULL,
    applied_by  TEXT    NOT NULL,
    duration_ms INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update BEFORE UPDATE ON {BOOKKEEPING_TABLE}
BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete BEFORE DELETE ON {BOOKKEEPING_TABLE}
BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
"""


@dataclass(frozen=True)
class Problem:
    kind: str  # checksum_mismatch | renamed | missing_file | out_of_order
    version: int
    detail: str

    def __str__(self) -> str:
        return f"{self.version:04d}: {self.detail}"


@dataclass(frozen=True)
class Applied:
    version: int
    name: str
    checksum: str
    applied_at: str
    applied_by: str
    duration_ms: int


@dataclass(frozen=True)
class StatusRow:
    version: int
    name: str
    state: str  # applied | pending | drifted | missing | out_of_order
    applied_at: str | None = None
    detail: str = ""


def check_history(files: list[Migration], applied: dict[int, Applied]) -> list[Problem]:
    """Compare what is on disk with what the database remembers. Pure; no I/O."""
    problems: list[Problem] = []
    by_version = {m.version: m for m in files}
    for v, row in sorted(applied.items()):
        m = by_version.get(v)
        if m is None:
            problems.append(Problem("missing_file", v, f"{v:04d}_{row.name}.sql was applied but is no longer on disk"))
        elif m.checksum != row.checksum:
            problems.append(Problem(
                "checksum_mismatch", v,
                f"{m.filename} changed after it was applied (recorded {row.checksum[:12]}…, now {m.checksum[:12]}…); "
                "write a new migration instead of editing an applied one",
            ))  # fmt: skip
        elif m.name != row.name:
            problems.append(Problem("renamed", v, f"applied as '{row.name}' but the file is now '{m.name}'"))
    newest = max(applied, default=0)
    for m in files:
        if m.version < newest and m.version not in applied:
            problems.append(Problem(
                "out_of_order", m.version,
                f"{m.filename} is older than the newest applied migration ({newest:04d}) but was never applied",
            ))  # fmt: skip
    return problems


class _Tx:
    """``BEGIN IMMEDIATE`` ... ``COMMIT`` (or ``ROLLBACK`` on any exception)."""

    def __init__(self, conn: sqlite3.Connection, commit: bool = True, before: Callable[[], None] | None = None):
        self.conn, self.commit, self.before = conn, commit, before

    def __enter__(self):
        if self.before:  # test seam: lets a test make another runner act *just before* we lock
            self.before()
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None and self.commit else "ROLLBACK")
        return False


class Migrator:
    def __init__(
        self,
        db_path: str | Path,
        directory: str | Path,
        *,
        actor: str = "unknown",
        clock: Callable[[], datetime] | None = None,
        busy_timeout: float = 30.0,
    ):
        self.db_path, self.directory, self.actor = str(db_path), Path(directory), actor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.busy_timeout = busy_timeout
        self._before_lock: Callable[[], None] | None = None  # test seam, see _Tx

    # -- plumbing ---------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=self.busy_timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _has_history(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (BOOKKEEPING_TABLE,)
        ).fetchone() is not None  # fmt: skip

    @staticmethod
    def _read_applied(conn: sqlite3.Connection) -> dict[int, Applied]:
        if not Migrator._has_history(conn):
            return {}
        rows = conn.execute(
            f"SELECT version, name, checksum, applied_at, applied_by, duration_ms FROM {BOOKKEEPING_TABLE}"
        ).fetchall()
        return {r["version"]: Applied(*tuple(r)) for r in rows}

    def _ensure_history(self, conn: sqlite3.Connection) -> None:
        for stmt in split_statements(BOOKKEEPING_SQL):
            conn.execute(stmt)

    # -- read-only views --------------------------------------------------------------------
    def verify(self) -> list[Problem]:
        files = load_migrations(self.directory)
        conn = self._connect()
        try:
            return check_history(files, self._read_applied(conn))
        finally:
            conn.close()

    def status(self) -> list[StatusRow]:
        files = load_migrations(self.directory)
        conn = self._connect()
        try:
            applied = self._read_applied(conn)
        finally:
            conn.close()
        problems = {(p.version): p for p in check_history(files, applied)}
        rows: list[StatusRow] = []
        for m in files:
            p = problems.get(m.version)
            row = applied.get(m.version)
            if p and p.kind == "out_of_order":
                rows.append(StatusRow(m.version, m.name, "out_of_order", None, p.detail))
            elif p:
                rows.append(StatusRow(m.version, m.name, "drifted", row.applied_at if row else None, p.detail))
            elif row:
                rows.append(StatusRow(m.version, m.name, "applied", row.applied_at))
            else:
                rows.append(StatusRow(m.version, m.name, "pending"))
        on_disk = {m.version for m in files}
        for v, row in sorted(applied.items()):
            if v not in on_disk:
                rows.append(StatusRow(v, row.name, "missing", row.applied_at, problems[v].detail))
        return sorted(rows, key=lambda r: r.version)

    # -- the write path ---------------------------------------------------------------------
    def migrate(self, target: int | None = None, *, dry_run: bool = False) -> list[Applied]:
        """Apply pending migrations in order. Returns what was (or, for a dry run, would be) applied."""
        files = load_migrations(self.directory)
        if target is not None and target not in {m.version for m in files}:
            raise MigrationError(f"target version {target} does not exist")
        conn = self._connect()
        try:
            if dry_run:
                return self._dry_run(conn, files, target)
            results: list[Applied] = []
            while True:
                with _Tx(conn, before=self._before_lock):  # lock first, then look: history is re-read under the lock
                    self._ensure_history(conn)
                    applied = self._read_applied(conn)
                    problems = check_history(files, applied)
                    if problems:
                        raise IntegrityProblem(problems)
                    pending = [m for m in files if m.version not in applied]
                    if target is not None:
                        pending = [m for m in pending if m.version <= target]
                    if not pending:
                        return results
                    results.append(self._apply(conn, pending[0]))
        finally:
            conn.close()

    def _dry_run(self, conn, files, target) -> list[Applied]:
        results: list[Applied] = []
        with _Tx(conn, commit=False):  # everything below is rolled back, even on success
            self._ensure_history(conn)
            applied = self._read_applied(conn)
            problems = check_history(files, applied)
            if problems:
                raise IntegrityProblem(problems)
            for m in files:
                if m.version in applied or (target is not None and m.version > target):
                    continue
                results.append(self._apply(conn, m))
        return results

    def _apply(self, conn: sqlite3.Connection, m: Migration) -> Applied:
        started = time.perf_counter()
        policy = Authorizer(allow_destructive=m.allow_destructive)
        conn.set_authorizer(policy)
        try:
            for n, stmt in enumerate(split_statements(m.sql), start=1):
                try:
                    conn.execute(stmt)
                except sqlite3.Error as e:
                    if policy.reason:
                        raise PolicyViolation(f"{m.filename}, statement {n}: {policy.reason}") from None
                    raise MigrationFailed(m.version, m.name, f"statement {n}: {e}") from None
        finally:
            conn.set_authorizer(None)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = tuple(violations[0])
            raise MigrationFailed(
                m.version, m.name,
                f"{len(violations)} foreign-key violation(s) after the migration, first in table {first[0]!r}",
            )  # fmt: skip
        elapsed = int((time.perf_counter() - started) * 1000)
        applied_at = self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            f"INSERT INTO {BOOKKEEPING_TABLE} (version, name, checksum, applied_at, applied_by, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (m.version, m.name, m.checksum, applied_at, self.actor, elapsed),
        )
        return Applied(m.version, m.name, m.checksum, applied_at, self.actor, elapsed)


def next_version(directory: str | Path) -> int:
    return len(load_migrations(directory)) + 1
