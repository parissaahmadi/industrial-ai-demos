"""Command line: serve the API, initialise a database, issue tokens, or run a scripted demo."""

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

from .db import SEED_USERS, connect
from .service import Flow


def _flow(path: str) -> Flow:
    return Flow(connect(path), clock=lambda: datetime.now(UTC))


def _issue_all(flow: Flow, out) -> dict[str, str]:
    tokens = {}
    for username, _name, _role in SEED_USERS:
        tokens[username] = flow.issue_token(username)
    for u, t in tokens.items():
        print(f"  {u:6} {t}", file=out)
    return tokens


def cmd_init_db(a) -> int:
    flow = _flow(a.db)
    print(f"Database ready: {a.db}\nTokens (shown once, only hashes are stored):", file=sys.stderr)
    _issue_all(flow, sys.stderr)
    return 0


def cmd_issue_token(a) -> int:
    print(_flow(a.db).issue_token(a.username))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    from .api import create_app

    flow = _flow(a.db)
    if a.db == ":memory:":
        print("In-memory database. Tokens (shown once):", file=sys.stderr)
        _issue_all(flow, sys.stderr)
    uvicorn.run(create_app(flow), host=a.host, port=a.port, log_level="warning")
    return 0


class _Http:
    def __init__(self, base: str, token: str | None = None):
        self.base, self.token = base, token

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        if self.token:
            req.add_header("authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def run_demo(out=sys.stdout) -> int:
    """Start a real uvicorn server on a free port and walk one costly request through it."""
    import uvicorn

    from .api import create_app

    flow = Flow(connect(":memory:"), clock=lambda: datetime.now(UTC))
    tokens = {u: flow.issue_token(u) for u, _n, _r in SEED_USERS}
    srv = uvicorn.Server(uvicorn.Config(create_app(flow), host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        th.join(0.01)
    base = f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    users = {u: _Http(base, t) for u, t in tokens.items()}

    def step(who, method, path, body=None, note=""):
        st, j = users[who].call(method, path, body)
        brief = (
            f"status={j['status']} version={j['version']}" if isinstance(j, dict) and "status" in j and "version" in j
            else j.get("detail", "") if isinstance(j, dict) else ""
        )  # fmt: skip
        print(f"{who:6} {method:5} {path:22} -> {st} {brief}  {note}".rstrip(), file=out)
        return st, j

    try:
        _, r = step("sub.a", "POST", "/requests", {"title": "Replace hydraulic pump seal", "asset_id": "AST-0042",
                    "priority": "high", "estimated_cost": 80_000_000}, "(cost > 50M limit)")  # fmt: skip
        rid = r["id"]
        step("sub.a", "POST", f"/requests/{rid}/submit", {"version": 1})
        step("sub.b", "GET", f"/requests/{rid}", None, "(other submitter)")
        step("sub.a", "POST", f"/requests/{rid}/decision", {"version": 2, "action": "approve"}, "(own request)")
        step("rev.c", "POST", f"/requests/{rid}/decision", {"version": 1, "action": "approve"}, "(stale version)")
        step("rev.c", "POST", f"/requests/{rid}/decision", {"version": 2, "action": "approve"}, "(first stage)")
        step("rev.c", "POST", f"/requests/{rid}/decision", {"version": 3, "action": "approve"}, "(same person again)")
        step("rev.d", "POST", f"/requests/{rid}/decision", {"version": 3, "action": "approve"}, "(reviewer cannot finish)")
        step("sup.e", "POST", f"/requests/{rid}/decision", {"version": 3, "action": "approve"}, "(final)")
        _, ev = step("sub.a", "GET", f"/requests/{rid}/events")
        print("audit trail:", file=out)
        for n, e in enumerate(ev["events"], 1):
            print(f"  #{n} {e['actor']:6} {e['action']:10} {e['from_status'] or '-':10} -> {e['to_status']}", file=out)
    finally:
        srv.should_exit = True
        th.join(5)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="workorder-flow", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--db", default="workorders.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("init-db", help="create the database and print tokens once")
    s.add_argument("--db", default="workorders.db")
    s.set_defaults(fn=cmd_init_db)
    s = sub.add_parser("issue-token", help="issue an extra token for a user")
    s.add_argument("username")
    s.add_argument("--db", default="workorders.db")
    s.set_defaults(fn=cmd_issue_token)
    s = sub.add_parser("demo", help="scripted scenario over real HTTP")
    s.set_defaults(fn=lambda a: run_demo())
    a = p.parse_args(argv)
    return a.fn(a)
