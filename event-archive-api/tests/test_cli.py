import json
import sqlite3
import sys
from datetime import UTC, datetime

from event_archive import cli
from event_archive.db import SEED_USERS, connect
from event_archive.service import Archive
from event_archive.transcribe import MEDIA_TYPE, make_fakevox
from helpers import ev


def test_parser_knows_every_command():
    p = cli.build_parser()
    for cmd in ("init-db", "serve", "worker", "verify-audit", "demo"):
        assert p.parse_args([cmd]).cmd == cmd
    a = p.parse_args(["serve", "--worker", "--port", "9000"])
    assert (a.worker, a.port, a.host) == (True, 9000, "127.0.0.1")


class Capture:
    def __enter__(self):
        import io

        self.out, self.err, self._o, self._e = io.StringIO(), io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self._o, self._e


def test_demo_runs_and_shows_the_main_features():
    with Capture() as c:
        assert cli.main(["demo"]) == 0
    text = c.out.getvalue()
    for needle in ["loaded fictional plant data", "[[pump]]", "[[خنک]]", "needs_review", "after a human correction",
                   '"ok": true', "broken_at", "row content does not match its hash"]:
        assert needle in text, needle
    assert text.count("(no results)") == 2  # the hostile query and the redacted event


def test_demo_is_deterministic():
    outs = []
    for _ in range(2):
        with Capture() as c:
            cli.main(["demo"])
        outs.append(c.out.getvalue())
    assert outs[0] == outs[1]


def test_init_db_creates_a_file_and_prints_each_token_once(tmp_path):
    db = tmp_path / "x.sqlite3"
    with Capture() as c:
        assert cli.main(["init-db", "--db", str(db)]) == 0
    assert db.exists() and c.out.getvalue() == ""
    lines = [ln.split() for ln in c.err.getvalue().splitlines() if ln.startswith("  ")]
    assert [ln[0] for ln in lines] == [u for u, _, _ in SEED_USERS]
    assert len({ln[2] for ln in lines}) == len(SEED_USERS)
    rows = sqlite3.connect(db).execute("SELECT token_sha256 FROM api_tokens").fetchall()
    assert len(rows) == len(SEED_USERS) and all(ln[2] not in [r[0] for r in rows] for ln in lines)


def test_verify_audit_exit_codes(tmp_path):
    db = tmp_path / "x.sqlite3"
    with Capture():
        cli.main(["init-db", "--db", str(db)])
    with Capture() as c:
        assert cli.main(["verify-audit", "--db", str(db)]) == 0
    good = json.loads(c.out.getvalue())
    assert good["ok"] and good["count"] == len(SEED_USERS)
    with Capture() as c:
        assert cli.main(["verify-audit", "--db", str(db), "--expected-head", "0" * 64]) == 1
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit SET actor = 'x' WHERE id = 2")
    conn.commit()
    conn.close()
    with Capture() as c:
        assert cli.main(["verify-audit", "--db", str(db)]) == 1
    assert json.loads(c.out.getvalue())["broken_at"] == 2


def test_worker_once_processes_the_queue(tmp_path):
    db = str(tmp_path / "x.sqlite3")
    arch = Archive(connect(db), lambda: datetime.now(UTC))
    op = arch.authenticate(arch.issue_token("op.b"))
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    e, _ = arch.ingest(op, ev(occurred_at=now))
    arch.attach_recording(op, e["id"], make_fakevox([("hello", 0.9)], "en"), MEDIA_TYPE)
    with Capture() as c:
        assert cli.main(["worker", "--db", db, "--once"]) == 0
    assert json.loads(c.out.getvalue())["done"] == 1
    assert arch.get_transcript(op, e["id"])["status"] == "done"
