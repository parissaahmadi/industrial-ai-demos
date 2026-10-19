"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate6.py            (uses `pytest` if importable via uv: set RUNNER, default `python -m pytest -q -x`)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()

MUTATIONS = [
    ("states.py", "cost <= reviewer_limit", "cost < reviewer_limit", "limit boundary: cost == limit must not need a supervisor"),
    ("states.py", "if status == REVIEWED or cost", "if cost", "second stage must always give final approval"),
    ("states.py", "    if actor == owner:\n", "    if False:\n", "owner may decide own request"),
    ("states.py", "status == REVIEWED and actor == reviewed_by", "False", "same reviewer may finish own first stage"),
    ("states.py", "if role not in allowed:", "if False:", "role check removed"),
    ("states.py", "return Outcome(RETURNED, clear_reviewed_by=True)", "return Outcome(RETURNED)", "return keeps stale reviewed_by"),
    ("service.py", "if row[\"version\"] != version:", "if False:", "optimistic concurrency removed"),
    ("service.py", "WHERE id = ? AND version = ?", "WHERE id = ?", "write ignores version (lost update)"),
    ("service.py", "        if row[\"status\"] == st.DRAFT:\n            return False\n", "", "drafts visible to everyone"),
    ("service.py", "self.db.execute(\"ROLLBACK\" if exc_type else \"COMMIT\")", "self.db.execute(\"COMMIT\")", "failed writes are committed"),
    ("service.py", "if row[\"owner\"] != p.username:", "if False:", "non-owners may edit/submit/withdraw"),
    ("db.py", "return hashlib.sha256(token.encode(\"utf-8\")).hexdigest()", "return token", "tokens stored in clear text"),
    ("db.py", "BEFORE UPDATE ON audit_events\nBEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;", "BEFORE UPDATE ON audit_events\nBEGIN SELECT 1; END;", "audit rows can be rewritten"),
    ("api.py", "if len(raw) > MAX_BODY:", "if False:", "body size limit removed"),
    ("api.py", '"cache-control": "no-store"', '"cache-control": "public"', "responses cacheable"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True).returncode


def main() -> int:
    survived = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (fname, old, new, why) in enumerate(MUTATIONS, 1):
            tree = Path(tmp) / f"m{i}"
            shutil.copytree(ROOT / "src", tree / "src")
            shutil.copytree(ROOT / "tests", tree / "tests")
            shutil.copy(ROOT / "pyproject.toml", tree)
            target = tree / "src" / "workorder_flow" / fname
            text = target.read_text()
            if old not in text:
                print(f"{i:2} SETUP ERROR  pattern not found in {fname}: {old!r}")
                return 2
            target.write_text(text.replace(old, new, 1))
            killed = run(tree) != 0
            print(f"{i:2} {'killed  ' if killed else 'SURVIVED'} {fname:11} {why}")
            if not killed:
                survived.append(why)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
