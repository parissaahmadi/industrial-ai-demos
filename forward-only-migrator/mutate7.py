"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate7.py        (RUNNER="uv run pytest -q -x" python mutate7.py to choose the test command)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()

# (file, [(old, new), ...], what the bug is)
MUTATIONS = [
    ("runner.py", [("elif m.checksum != row.checksum:", "elif False:")], "edited applied files are not detected"),
    ("runner.py", [("elif m.name != row.name:", "elif False:")], "renamed applied files are not detected"),
    ("runner.py", [("        if m is None:\n", "        if m is None:\n            continue\n        if m is None:\n")], "deleted applied files are ignored"),
    ("runner.py", [("if m.version < newest and m.version not in applied:", "if False:")], "out-of-order files are not detected"),
    ("runner.py", [('self.conn.execute("COMMIT" if exc_type is None and self.commit else "ROLLBACK")', 'self.conn.execute("COMMIT")')], "failed migrations are committed"),
    ("runner.py", [("with _Tx(conn, commit=False):", "with _Tx(conn):")], "dry run commits"),
    ("runner.py", [('self.conn.execute("BEGIN IMMEDIATE")', 'self.conn.execute("BEGIN")')], "write lock taken lazily, not up front"),
    ("runner.py", [("                with _Tx(conn, before=self._before_lock):  # lock first, then look: history is re-read under the lock\n                    self._ensure_history(conn)\n                    applied = self._read_applied(conn)\n",
                    "                applied = self._read_applied(conn)  # BUG: read before the lock\n                with _Tx(conn, before=self._before_lock):\n                    self._ensure_history(conn)\n")],
     "history read before taking the lock (double apply)"),  # fmt: skip
    ("runner.py", [('violations = conn.execute("PRAGMA foreign_key_check").fetchall()', "violations = []")], "foreign-key check after a migration removed"),
    ("runner.py", [("pending = [m for m in pending if m.version <= target]", "pass")], "--target ignored"),
    ("runner.py", [("(m.version, m.name, m.checksum, applied_at, self.actor, elapsed)", '(m.version, m.name, "0" * 64, applied_at, self.actor, elapsed)')], "wrong checksum recorded"),
    ("runner.py", [("        conn.set_authorizer(None)\n", "        pass\n")], "restriction not lifted after a migration"),
    ("runner.py", [("BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;\nCREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete", "BEGIN SELECT 1; END;\nCREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete")], "history rows can be updated"),
    ("policy.py", [("if action in _WRITES and arg1 == protected:", "if False:")], "migrations may write to the history table"),
    ("policy.py", [("if action in _TOUCHES_TABLE_ARG2 and arg2 == protected:", "if False:")], "migrations may add triggers/columns to the history table"),
    ("policy.py", [('if self.allow_destructive or dbname == "temp":', "if True:")], "destructive statements always allowed"),
    ("policy.py", [('if self.allow_destructive or dbname == "temp":', 'if dbname == "temp":')], "destructive directive ignored"),
    ("policy.py", [("    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE,\n}", "    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE, _S.SQLITE_PRAGMA,\n}")], "every PRAGMA allowed"),
    ("policy.py", [('if action == _S.SQLITE_PRAGMA and (arg1 or "").lower() in _READONLY_PRAGMAS:', "if False:")], "ADD COLUMN with CHECK wrongly denied (integrity pragmas blocked)"),
    ("policy.py", [("    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE,\n}", "    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE, _S.SQLITE_TRANSACTION, _S.SQLITE_SAVEPOINT,\n}")], "migration may control transactions"),
    ("policy.py", [("    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE,\n}", "    _S.SQLITE_ANALYZE, _S.SQLITE_RECURSIVE, _S.SQLITE_ATTACH,\n}")], "ATTACH allowed"),
    ("policy.py", [('if (arg2 or "").lower() in _DENIED_FUNCTIONS:', "if False:")], "load_extension allowed"),
    ("files.py", [("if version != expected:", "if False:")], "gaps in version numbers allowed"),
    ("files.py", [("if version in found:", "if False:")], "duplicate versions allowed"),
    ("files.py", [('allow_destructive="allow-destructive" in _directives(sql, path.name),', "allow_destructive=False,")], "directive never recognised"),
    ("files.py", [('return hashlib.sha256(data.replace(b"\\r\\n", b"\\n")).hexdigest()', "return hashlib.sha256(data).hexdigest()")], "checksum depends on line endings"),
    ("sqlsplit.py", [("if ch == \";\" and sqlite3.complete_statement(sql[start : i + 1]):", 'if ch == ";":')], "statements split inside trigger bodies"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True).returncode


def main() -> int:
    survived = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (fname, edits, why) in enumerate(MUTATIONS, 1):
            tree = Path(tmp) / f"m{i}"
            shutil.copytree(ROOT / "src", tree / "src")
            for extra in ("tests", "examples"):
                shutil.copytree(ROOT / extra, tree / extra)
            shutil.copy(ROOT / "pyproject.toml", tree)
            target = tree / "src" / "fwd_migrator" / fname
            text = target.read_text()
            for old, new in edits:
                if old not in text:
                    print(f"{i:2} SETUP ERROR  pattern not found in {fname}: {old[:60]!r}")
                    return 2
                text = text.replace(old, new, 1)
            target.write_text(text)
            killed = run(tree) != 0
            print(f"{i:2} {'killed  ' if killed else 'SURVIVED'} {fname:11} {why}")
            if not killed:
                survived.append(why)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
