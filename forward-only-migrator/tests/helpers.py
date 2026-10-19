import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fwd_migrator.runner import Migrator

NOW = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "migrations"

BASE = {
    "0001_create_t.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);",
    "0002_add_u.sql": "CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER REFERENCES t (id));",
}


def write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def make(tmp_path: Path, files: dict[str, str], **kw) -> Migrator:
    d = write(tmp_path / "migrations", files)
    return Migrator(tmp_path / "db.sqlite3", d, clock=lambda: NOW, actor="ops.a", **kw)


def query(m: Migrator, sql: str, args=()):
    conn = sqlite3.connect(m.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def tables(m: Migrator) -> set[str]:
    rows = query(m, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}
