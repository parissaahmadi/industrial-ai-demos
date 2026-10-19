"""Real sockets: a uvicorn server on a free port, driven with urllib."""

import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from helpers import NEW, TOKENS, make_flow
from workorder_flow.api import create_app
from workorder_flow.cli import run_demo


@pytest.fixture
def server():
    import uvicorn

    flow = make_flow()
    srv = uvicorn.Server(uvicorn.Config(create_app(flow), host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        th.join(0.01)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True
    th.join(5)


def call(base, method, path, user=None, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if user:
        req.add_header("authorization", f"Bearer {TOKENS[user]}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def test_over_a_real_socket(server):
    assert call(server, "GET", "/health")[0] == 200
    st, h, j = call(server, "GET", "/me")
    assert st == 401 and "Bearer" in h["www-authenticate"]
    st, h, j = call(server, "POST", "/requests", "sub.a", NEW)
    assert st == 201 and h["location"] == f"/requests/{j['id']}"
    st, _, j = call(server, "POST", f"/requests/{j['id']}/submit", "sub.a", {"version": 1})
    assert (st, j["status"]) == (200, "submitted")
    st, _, j = call(server, "POST", f"/requests/{j['id']}/decision", "rev.c", {"version": 2, "action": "approve"})
    assert (st, j["status"]) == (200, "approved")  # cost within the reviewer limit


def test_bad_json_over_a_real_socket(server):
    req = urllib.request.Request(server + "/requests", method="POST", data=b"{oops")
    req.add_header("authorization", f"Bearer {TOKENS['sub.a']}")
    req.add_header("content-type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 422


def test_scripted_demo_runs_and_ends_approved():
    out = io.StringIO()
    assert run_demo(out) == 0
    text = out.getvalue()
    assert "status=approved version=4" in text and "#4 sup.e" in text
