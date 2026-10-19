"""Command line for the migrator. Exit codes: 0 ok, 1 error, 2 history problem found by ``verify``."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .errors import IntegrityProblem, MigrationError
from .runner import Migrator, next_version

TEMPLATE = """-- {name}
-- Forward-only: once this file is applied it must never be edited.
-- Add '-- migrate: allow-destructive' as the first line if it needs DROP or DELETE.

"""


def _migrator(a) -> Migrator:
    return Migrator(a.db, a.dir, actor=a.actor)


def cmd_status(a, out) -> int:
    rows = _migrator(a).status()
    for r in rows:
        extra = f"  {r.applied_at}" if r.applied_at and r.state == "applied" else ""
        print(f"{r.version:04d}  {r.state:<12} {r.name}{extra}", file=out)
        if r.detail:
            print(f"      {r.detail}", file=out)
    return 0


def cmd_verify(a, out) -> int:
    problems = _migrator(a).verify()
    if not problems:
        print("history OK", file=out)
        return 0
    for p in problems:
        print(f"PROBLEM {p.kind}: {p}", file=out)
    return 2


def cmd_up(a, out) -> int:
    done = _migrator(a).migrate(a.target, dry_run=a.dry_run)
    verb = "would apply" if a.dry_run else "applied"
    for r in done:
        print(f"{verb} {r.version:04d}_{r.name}", file=out)
    if not done:
        print("nothing to do", file=out)
    elif a.dry_run:
        print("dry run: everything was rolled back", file=out)
    return 0


def cmd_new(a, out) -> int:
    slug = re.sub(r"[^a-z0-9]+", "_", a.name.lower()).strip("_")
    if not slug:
        raise MigrationError("name must contain letters or digits")
    path = Path(a.dir) / f"{next_version(a.dir):04d}_{slug}.sql"
    path.write_text(TEMPLATE.format(name=slug), encoding="utf-8")
    print(f"created {path}", file=out)
    return 0


def run_demo(out=None) -> int:
    """A scripted tour on a throw-away directory: apply, dry-run, tamper, fail, police."""
    out = out or sys.stdout
    example = Path(__file__).resolve().parents[2] / "examples" / "migrations"
    with tempfile.TemporaryDirectory() as tmp:
        mdir, db = Path(tmp) / "migrations", str(Path(tmp) / "plant.sqlite3")
        shutil.copytree(example, mdir)
        m = Migrator(db, mdir, actor="ops.a", clock=lambda: datetime(2026, 10, 12, 6, 0, tzinfo=UTC))

        def show(title):
            print(f"\n$ {title}", file=out)

        def attempt(fn):
            try:
                fn()
            except MigrationError as e:
                print(f"ERROR [{e.code}] {e}", file=out)

        def status():
            for r in m.status():
                print(f"  {r.version:04d} {r.state:<12} {r.name}", file=out)

        show("fwd-migrate up --dry-run   (SQL runs, then everything is rolled back)")
        for r in m.migrate(dry_run=True):
            print(f"  would apply {r.version:04d}_{r.name}", file=out)
        conn = sqlite3.connect(db)
        left = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        conn.close()
        print(f"  objects in the database afterwards: {left}", file=out)

        show("fwd-migrate up")
        for r in m.migrate():
            print(f"  applied {r.version:04d}_{r.name}", file=out)
        show("fwd-migrate up   (again)")
        print(f"  applied {len(m.migrate())} migration(s)", file=out)

        show("edit an applied migration, then: fwd-migrate verify")
        victim = mdir / "0003_add_asset_site_and_index.sql"
        original = victim.read_bytes()
        victim.write_bytes(original + b"\nCREATE INDEX sneaky ON assets (tag);\n")
        for p in m.verify():
            print(f"  PROBLEM {p.kind}: {p}", file=out)
        attempt(m.migrate)
        victim.write_bytes(original)
        print("  (restored)", file=out)
        print(f"  verify after restore: {m.verify() or 'history OK'}", file=out)

        show("add 0006 that breaks half-way")
        bad = mdir / "0006_add_meter_readings.sql"
        bad.write_text("CREATE TABLE meter_readings (id INTEGER PRIMARY KEY, asset_id INTEGER);\nINSERT INTO no_such_table VALUES (1);\n")
        attempt(m.migrate)
        conn = sqlite3.connect(db)
        exists = conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'meter_readings'").fetchone()[0]
        conn.close()
        print(f"  meter_readings table exists after the failure: {bool(exists)}", file=out)
        status()

        show("fix 0006 (it was never applied, so editing it is allowed) and run again")
        bad.write_text("CREATE TABLE meter_readings (id INTEGER PRIMARY KEY, asset_id INTEGER REFERENCES assets (id), value REAL);\n")
        for r in m.migrate():
            print(f"  applied {r.version:04d}_{r.name}", file=out)

        show("0007 tries DROP TABLE without the directive, 0007 is then rewritten to forge history")
        evil = mdir / "0007_cleanup.sql"
        evil.write_text("DROP TABLE meter_readings;\n")
        attempt(m.migrate)
        evil.write_text("DELETE FROM schema_migrations;\n")
        attempt(m.migrate)
        status()
    return 0


def main(argv=None, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    p = argparse.ArgumentParser(prog="fwd-migrate", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="app.sqlite3", help="SQLite file (default: app.sqlite3)")
    common.add_argument("--dir", default="migrations", help="migrations directory (default: ./migrations)")
    common.add_argument("--actor", default="unknown", help="who is running this; stored in schema_migrations")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common], help="show every migration's state").set_defaults(fn=cmd_status)
    sub.add_parser("verify", parents=[common], help="exit 2 if history and files disagree").set_defaults(fn=cmd_verify)
    s = sub.add_parser("up", parents=[common], help="apply pending migrations")
    s.add_argument("--target", type=int, help="stop after this version")
    s.add_argument("--dry-run", action="store_true", help="run the SQL, then roll everything back")
    s.set_defaults(fn=cmd_up)
    s = sub.add_parser("new", parents=[common], help="create the next numbered file")
    s.add_argument("name")
    s.set_defaults(fn=cmd_new)
    sub.add_parser("demo", help="scripted tour on a throw-away directory").set_defaults(fn=lambda a, o: run_demo(o))
    a = p.parse_args(argv)
    try:
        return a.fn(a, out)
    except IntegrityProblem as e:
        print(f"refusing to continue [{e.code}]: {e}", file=err)
        return 1
    except MigrationError as e:
        print(f"error [{e.code}]: {e}", file=err)
        return 1
