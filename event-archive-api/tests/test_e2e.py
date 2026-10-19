"""Real sockets, real processes, real files: nothing in-process is shared with the code under test
except what a deployment would share (the SQLite file)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from event_archive import audit
from event_archive.api import create_app
from event_archive.db import connect
from event_archive.service import Archive
from event_archive.transcribe import MEDIA_TYPE, MockTranscriber, make_fakevox
from helpers import GOOD_AUDIO, NOW, TOKENS, ev, make_archive

SRC = str(Path(__file__).resolve().parent.parent / "src")


def call(base, method, path, token=None, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, method=method, data=data)
    req.add_header("content-type", ctype)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


@pytest.fixture
def server():
    import uvicorn

    arch = make_archive()
    srv = uvicorn.Server(uvicorn.Config(create_app(arch), host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        th.join(0.01)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True
    th.join(5)


EVENT = {"occurred_at": "2026-10-12T06:00:00Z", "asset_id": "PMP-0001", "kind": "alarm", "severity": "warning",
         "message": "نشت روغن از آب‌بند", "tags": ["نشت"]}  # fmt: skip


def test_whole_flow_over_a_real_socket(server):
    op, view, aud, adm = (TOKENS[u] for u in ("op.b", "view.a", "aud.e", "adm.d"))
    assert call(server, "GET", "/health")[0] == 200
    st, h, j = call(server, "POST", "/events", op, EVENT)
    assert st == 201 and h["location"] == f"/events/{j['id']}"
    eid = j["id"]
    st, _, j = call(server, "PUT", f"/events/{eid}/recording", op, raw=GOOD_AUDIO, ctype=MEDIA_TYPE)
    assert st == 201
    assert call(server, "POST", "/worker/run", op)[2]["done"] == 1
    st, _, j = call(server, "GET", "/events/search?q=%D9%86%D8%B4%D8%AA&in=message", view)  # «نشت»
    assert st == 200 and [i["id"] for i in j["items"]] == [eid]
    assert call(server, "GET", "/events/search?q=stopping&in=transcript", view)[2]["count"] == 1
    assert call(server, "POST", f"/events/{eid}/redact", adm, {"reason": "test of redaction"})[0] == 200
    assert call(server, "GET", "/events/search?q=stopping", view)[2]["count"] == 0
    st, _, j = call(server, "GET", "/audit/verify", aud)
    assert st == 200 and j["ok"] and j["count"] >= 7
    st, h, j = call(server, "GET", "/audit", view)
    assert st == 403 and j["type"] == "forbidden"


def test_bad_json_and_big_upload_over_a_real_socket(server):
    op = TOKENS["op.b"]
    assert call(server, "POST", "/events", op, raw=b"{oops")[0] == 422
    eid = call(server, "POST", "/events", op, EVENT)[2]["id"]
    # The server refuses on the *declared* length without reading the body. A client that keeps pushing
    # 1 MB into a connection the server has already answered gets a reset (that race made an earlier
    # version of this test flaky), so send the headers and a few bytes only and read the answer.
    host, port = server.removeprefix("http://").split(":")
    head = (f"PUT /events/{eid}/recording HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {op}\r\n"
            f"Content-Type: {MEDIA_TYPE}\r\nContent-Length: 1100000\r\nConnection: close\r\n\r\nFAKEVOX1\n")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head.encode())
        buf = b""
        while b"payload_too_large" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    assert buf.startswith(b"HTTP/1.1 413") and b"payload_too_large" in buf


# ------------------------------------------------------------------ two connections, one file
class Counting(MockTranscriber):
    def __init__(self):
        super().__init__()
        self.n = 0
        self.lock = threading.Lock()

    def transcribe(self, audio):
        with self.lock:
            self.n += 1
        time.sleep(0.01)  # widen the window in which a second worker could grab the same job
        return super().transcribe(audio)


def file_archive(path, transcriber=None):
    return Archive(connect(str(path)), lambda: NOW, transcriber)


def test_two_workers_on_one_database_never_share_a_job(tmp_path):
    path = tmp_path / "a.sqlite3"
    boss = file_archive(path)
    op = boss.authenticate(boss.issue_token("op.b"))
    for i in range(30):
        e, _ = boss.ingest(op, ev(message=f"m{i}"))
        boss.attach_recording(op, e["id"], make_fakevox([(f"clip {i}", 0.9)]), MEDIA_TYPE)
    engines = [Counting(), Counting()]
    workers = [file_archive(path, t) for t in engines]
    totals = []

    def loop(w):
        done = 0
        while True:
            s = w.run_worker(limit=3)
            if not s["claimed"]:
                break
            done += s["done"]
        totals.append(done)

    ts = [threading.Thread(target=loop, args=(w,)) for w in workers]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert sum(totals) == 30 and sum(t.n for t in engines) == 30  # each job transcribed exactly once
    assert min(totals) > 0  # both really took part
    assert boss.stats(op)["recordings"] == {"done": 30}
    assert audit.verify(boss.db)["ok"]


def test_concurrent_writers_keep_the_audit_chain_linear(tmp_path):
    path = tmp_path / "a.sqlite3"
    first = file_archive(path)
    op = first.authenticate(first.issue_token("op.b"))
    archives = [file_archive(path) for _ in range(4)]
    errors = []

    def write(arch, k):
        try:
            for i in range(15):
                arch.ingest(op, ev(message=f"w{k}-{i}", source="s", source_ref=f"{k}-{i}"))
        except Exception as e:  # pragma: no cover - the assertion below reports it
            errors.append(e)

    ts = [threading.Thread(target=write, args=(a, k)) for k, a in enumerate(archives)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert errors == []
    r = audit.verify(first.db)
    assert r["ok"] and r["count"] == 1 + 60  # token.issue + 60 ingests
    assert first.stats(op)["events"] == 60


# ------------------------------------------------------------------ the CLI as a real process
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cli(*args, **kw):
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "event_archive", *args], capture_output=True, text=True, env=env, timeout=60, **kw)


def test_serve_with_worker_as_a_separate_process(tmp_path):
    db = str(tmp_path / "live.sqlite3")
    init = cli("init-db", "--db", db)
    assert init.returncode == 0
    tokens = {line.split()[0]: line.split()[2] for line in init.stderr.splitlines() if line.startswith("  ")}
    assert set(tokens) == set(TOKENS)
    port = free_port()
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "event_archive", "serve", "--db", db, "--port", str(port),
                             "--worker", "--interval", "0.2"], env=env, stderr=subprocess.PIPE)  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if call(base, "GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not start")
        eid = call(base, "POST", "/events", tokens["op.b"], EVENT)[2]["id"]
        assert call(base, "PUT", f"/events/{eid}/recording", tokens["op.b"], raw=GOOD_AUDIO, ctype=MEDIA_TYPE)[0] == 201
        for _ in range(100):  # the background worker picks it up
            t = call(base, "GET", f"/events/{eid}/transcript", tokens["view.a"])[2]
            if t["status"] == "done":
                break
            time.sleep(0.1)
        assert t["status"] == "done" and t["text"] == "bearing is hot stopping the pump"
    finally:
        proc.terminate()
        proc.wait(10)
    ver = cli("verify-audit", "--db", db)
    assert ver.returncode == 0 and json.loads(ver.stdout)["ok"] is True
