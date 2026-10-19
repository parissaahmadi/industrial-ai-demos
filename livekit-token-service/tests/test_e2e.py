"""Real sockets and a real process: nothing in-process is shared with the code under test."""

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
from helpers import KEY, SECRET, TOKENS, make_service
from livekit_tokens import jwt
from livekit_tokens.api import create_app

SRC = str(Path(__file__).resolve().parent.parent / "src")


def call(base, method, path, token=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, method=method, data=data)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, json.loads(e.read())


@pytest.fixture
def server():
    import uvicorn

    svc = make_service()
    svc.clock.t = time.time()  # the real clock: tokens are checked by wall time below
    srv = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        th.join(0.01)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True
    th.join(5)


def test_whole_flow_over_a_real_socket(server):
    tech, expert, sup, other = (TOKENS[u] for u in ("tech.a", "exp.c", "sup.d", "tech.b"))
    st, h, s = call(server, "POST", "/sessions", tech, {"asset_id": "PMP-0001", "reason": "bearing noise"})
    assert st == 201 and h["location"] == f"/sessions/{s['id']}"
    assert call(server, "POST", "/sessions", tech, {"asset_id": "PMP-0001", "reason": "again"})[0] == 409
    got = {}
    for who, tok in (("tech", tech), ("expert", expert), ("sup", sup)):
        st, h, r = call(server, "POST", f"/sessions/{s['id']}/token", tok, {"ttl_seconds": 120})
        assert st == 200 and h["cache-control"] == "no-store"
        got[who] = jwt.decode(r["token"], SECRET.encode(), now=time.time(), issuer=KEY)
        assert r["expires_in"] == 120
    assert {c["video"]["room"] for c in got.values()} == {s["room"]}
    assert got["sup"]["video"]["hidden"] and not got["sup"]["video"]["canPublish"]
    assert call(server, "POST", f"/sessions/{s['id']}/token", other)[0] == 403
    assert call(server, "POST", f"/sessions/{s['id']}/close", tech)[0] == 200
    assert call(server, "POST", f"/sessions/{s['id']}/token", expert)[0] == 409
    st, _, a = call(server, "GET", "/audit", TOKENS["adm.e"])
    assert st == 200 and [i["action"] for i in a["items"]].count("token.issue") == 3


def test_bad_json_and_declared_oversize_over_a_real_socket(server):
    assert call(server, "POST", "/sessions", TOKENS["tech.a"], raw=b"{oops")[0] == 422
    host, port = server.removeprefix("http://").split(":")
    head = (f"POST /sessions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKENS['tech.a']}\r\n"
            "Content-Type: application/json\r\nContent-Length: 1000000\r\nConnection: close\r\n\r\n{")  # fmt: skip
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head.encode())
        buf = b""
        while b"payload_too_large" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    assert buf.startswith(b"HTTP/1.1 413") and b"payload_too_large" in buf


def test_serve_as_a_separate_process_refuses_a_placeholder_secret(tmp_path):
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LIVEKIT_API_KEY": KEY,
           "LIVEKIT_API_SECRET": "change-me-" + "x" * 40, "LIVEKIT_URL": "ws://localhost:7880"}  # fmt: skip
    r = subprocess.run([sys.executable, "-m", "livekit_tokens", "serve", "--db", str(tmp_path / "x.db"), "--port", "0"],
                       env=env, capture_output=True, text=True, timeout=30)  # fmt: skip
    assert r.returncode == 2 and "placeholder" in r.stderr


def test_serve_as_a_separate_process_end_to_end(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "LIVEKIT_API_KEY": KEY,
           "LIVEKIT_API_SECRET": SECRET, "LIVEKIT_URL": "ws://localhost:7880"}  # fmt: skip
    db = str(tmp_path / "t.sqlite3")
    init = subprocess.run([sys.executable, "-m", "livekit_tokens", "init-db", "--db", db], env=env,
                          capture_output=True, text=True, timeout=30)  # fmt: skip
    assert init.returncode == 0
    toks = {ln.split()[0]: ln.split()[2] for ln in init.stdout.splitlines()[1:]}
    proc = subprocess.Popen([sys.executable, "-m", "livekit_tokens", "serve", "--db", db, "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if call(base, "GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start")
        _, _, s = call(base, "POST", "/sessions", toks["tech.a"], {"asset_id": "FAN-0003", "reason": "vibration"})
        st, _, r = call(base, "POST", f"/sessions/{s['id']}/token", toks["exp.c"])
        assert st == 200
        c = jwt.decode(r["token"], SECRET.encode(), now=time.time(), issuer=KEY)
        assert c["sub"] == "exp.c" and c["video"]["room"] == s["room"]
    finally:
        proc.terminate()
        proc.wait(10)
