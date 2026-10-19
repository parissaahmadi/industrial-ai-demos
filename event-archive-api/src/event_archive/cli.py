"""Command line: initialise a database, serve the API, run the transcription worker, verify the
audit chain, or run a scripted offline demo."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import UTC, datetime, timedelta

from . import audit as audit_log
from .db import SEED_USERS, connect
from .models import CorrectRequest, RedactRequest
from .sample_data import BASE, sample_events, sample_recordings
from .service import Archive, Principal
from .transcribe import MEDIA_TYPE


def _archive(path: str) -> Archive:
    return Archive(connect(path), clock=lambda: datetime.now(UTC))


def _issue_all(archive: Archive, out) -> None:
    for username, _name, role in SEED_USERS:
        print(f"  {username:7} {role:9} {archive.issue_token(username)}", file=out)


def cmd_init_db(a) -> int:
    archive = _archive(a.db)
    print(f"Database ready: {a.db}\nTokens (shown once, only hashes are stored):", file=sys.stderr)
    _issue_all(archive, sys.stderr)
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    from .api import create_app

    archive = _archive(a.db)
    if a.db == ":memory:":
        print("In-memory database. Tokens (shown once):", file=sys.stderr)
        _issue_all(archive, sys.stderr)
    if a.worker:
        threading.Thread(target=_worker_loop, args=(archive, a.interval), daemon=True).start()
    uvicorn.run(create_app(archive), host=a.host, port=a.port, log_level="warning")
    return 0


def _worker_loop(archive: Archive, interval: float, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            summary = archive.run_worker()
            if summary["claimed"]:
                print(json.dumps(summary), file=sys.stderr, flush=True)
        except Exception as e:  # keep the loop alive; the next tick retries
            print(f"worker error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        stop.wait(interval)


def cmd_worker(a) -> int:
    archive = _archive(a.db)
    if a.once:
        print(json.dumps(archive.run_worker()))
        return 0
    try:
        _worker_loop(archive, a.interval)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_verify_audit(a) -> int:
    result = audit_log.verify(connect(a.db), a.expected_head)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


# ----------------------------------------------------------------------------- demo
def _show(title: str, data) -> None:
    print(f"\n== {title}")
    print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))


def _hits(res: dict) -> str:
    return "\n".join(f"  #{i['id']:<3} {i['occurred_at'][11:16]} {i['asset_id']} {i['kind']:5} {i['severity']:8}"
                     f" {(i.get('snippet') or i['message'])[:70]}" for i in res["items"]) or "  (no results)"  # fmt: skip


def cmd_demo(a) -> int:
    box = {"now": BASE + timedelta(hours=7)}
    arch = Archive(connect(":memory:"), lambda: box["now"])
    op = Principal("op.b", "Sample Operator B", "operator")
    adm = Principal("adm.d", "Sample Admin D", "admin")
    view = Principal("view.a", "Sample Viewer A", "viewer")
    aud = Principal("aud.e", "Sample Auditor E", "auditor")

    ids = [arch.ingest(op, e)[0]["id"] for e in sample_events()]
    for idx, audio in sample_recordings():
        arch.attach_recording(op, ids[idx], audio, MEDIA_TYPE)
    _show("1. loaded fictional plant data", arch.stats(view))

    print("\n== 2. transcription worker (mock engine; one recording fails twice before it works)")
    elapsed = 0
    for wait in (30, 60, 5):  # back-off after attempt 1 is 30 s, after attempt 2 it is 60 s
        print(f"  t+{elapsed:>3}s  {json.dumps(arch.run_worker())}")
        box["now"] += timedelta(seconds=wait)
        elapsed += wait
    print(f"  t+{elapsed:>3}s  {json.dumps(arch.run_worker())}")
    for eid in (ids[12], ids[19]):
        r = arch.get(view, eid)["recording"]
        print(
            f"  event #{eid}: status={r['status']} attempts={r['attempts']} error={r['last_error']}"
        )

    _show("3. search: pump", _hits(arch.search(view, "pump", sort="relevance")))
    _show(
        "4. search with Persian digits and spelling: «PMP-۰۰۰۱ seal» and «خنک کننده»",
        _hits(arch.search(view, "PMP-۰۰۰۱ seal")),
    )
    print(_hits(arch.search(view, "خنک کننده")))
    _show(
        "5. search inside transcripts only: «روغن»",
        _hits(arch.search(view, "روغن", scope="transcript")),
    )
    _show(
        "6. filters: critical alarms of the day, newest first",
        _hits(arch.search(view, kind="alarm", severity="critical")),
    )
    _show(
        '7. hostile query is treated as plain words: «pump" OR NEAR(a b) message:x*»',
        _hits(arch.search(view, 'pump" OR NEAR(a b) message:x*')),
    )

    t = arch.get_transcript(view, ids[9])
    _show(
        "8. low-confidence transcript flagged for review",
        {k: t[k] for k in ("status", "confidence", "needs_review", "text")},
    )
    arch.correct_transcript(
        op,
        ids[9],
        CorrectRequest(version=t["version"], text="نشت روغن از آب‌بند؛ سطح روغن خیلی پایین است"),
    )
    t2 = arch.get_transcript(view, ids[9])
    _show(
        "9. after a human correction (machine text is kept)",
        {k: t2[k] for k in ("needs_review", "corrected", "text", "original_text")},
    )
    print(_hits(arch.search(view, "خیلی پایین", scope="transcript")))

    arch.redact(adm, ids[8], RedactRequest(reason="entered on the wrong asset"))
    _show(f"10. after redacting event #{ids[8]}", _hits(arch.search(view, "gearbox leak seal")))

    entries = arch.audit_entries(aud, limit=100)["items"]
    _show(
        f"11. audit trail (last 6 of {len(entries)} entries)",
        "\n".join(
            f"  #{e['id']:<3} {e['actor']:14} {e['action']:20} {e['target']:10} {e['hash'][:12]}"
            for e in entries[-6:]
        ),
    )
    print("  " + json.dumps(arch.audit_verify(aud)))

    arch.db.execute("DROP TRIGGER audit_no_update")  # an attacker with raw database access
    arch.db.execute("UPDATE audit SET actor = 'someone.else' WHERE id = 5")
    _show("12. after tampering with row 5 directly in the database", arch.audit_verify(aud))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="event-archive", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-db", help="create a database and issue one token per fictional user")
    s.add_argument("--db", default="archive.sqlite3")
    s.set_defaults(fn=cmd_init_db)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--db", default="archive.sqlite3")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument(
        "--worker", action="store_true", help="also run the transcription worker in a thread"
    )
    s.add_argument("--interval", type=float, default=2.0)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("worker", help="run the transcription worker as its own process")
    s.add_argument("--db", default="archive.sqlite3")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=float, default=2.0)
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser(
        "verify-audit", help="check the audit hash chain of a database (exit 1 if broken)"
    )
    s.add_argument("--db", default="archive.sqlite3")
    s.add_argument("--expected-head", help="head hash you saved earlier; detects removed tail rows")
    s.set_defaults(fn=cmd_verify_audit)
    s = sub.add_parser("demo", help="scripted offline demo with fictional data and a fixed clock")
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
