import io
import json
import sys
import time

from helpers import KEY, SECRET
from livekit_tokens import cli, jwt
from livekit_tokens.db import SEED_USERS, connect


class Capture:
    def __enter__(self):
        self.out, self.err, self._o, self._e = io.StringIO(), io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self._o, self._e


def env(monkeypatch, **over):
    base = {"LIVEKIT_API_KEY": KEY, "LIVEKIT_API_SECRET": SECRET, "LIVEKIT_URL": "ws://localhost:7880"}
    base.update(over)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_parser_knows_every_command():
    p = cli.build_parser()
    for cmd in ("init-db", "serve", "inspect-token", "demo"):
        assert p.parse_args([cmd]).cmd == cmd
    a = p.parse_args(["serve", "--port", "9000"])
    assert (a.port, a.host, a.db) == (9000, "127.0.0.1", "tokens.sqlite3")
    assert p.parse_args(["inspect-token", "abc", "--verify"]).verify is True


def test_init_db_prints_one_working_token_per_user(tmp_path, monkeypatch):
    env(monkeypatch)
    path = str(tmp_path / "t.sqlite3")
    with Capture() as c:
        assert cli.main(["init-db", "--db", path]) == 0
    lines = [ln.split() for ln in c.out.getvalue().splitlines()[1:]]
    assert [ln[0] for ln in lines] == [u for u, _, _ in SEED_USERS]
    from helpers import settings
    from livekit_tokens.service import Service

    svc = Service(connect(path), settings(), time.time)
    assert svc.authenticate(lines[2][2]).role == "expert"
    assert SECRET not in c.out.getvalue()


def test_init_db_twice_adds_tokens_without_duplicating_users(tmp_path, monkeypatch):
    env(monkeypatch)
    path = str(tmp_path / "t.sqlite3")
    with Capture():
        cli.main(["init-db", "--db", path])
        cli.main(["init-db", "--db", path])
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == len(SEED_USERS)
    assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 2 * len(SEED_USERS)


def test_bad_configuration_exits_2_and_names_the_problem(tmp_path, monkeypatch):
    env(monkeypatch, LIVEKIT_API_SECRET="change-me-" + "x" * 40)
    with Capture() as c:
        assert cli.main(["init-db", "--db", str(tmp_path / "t.sqlite3")]) == 2
    assert "placeholder" in c.err.getvalue() and "change-me" not in c.err.getvalue()


def test_inspect_token_without_verify_says_the_signature_was_not_checked(monkeypatch):
    token = jwt.encode({"exp": int(time.time()) + 100, "sub": "x"}, b"whatever-secret")
    with Capture() as c:
        assert cli.main(["inspect-token", token]) == 0
    out = c.out.getvalue()
    assert "NOT checked" in out and '"sub": "x"' in out and "expires in" in out


def test_inspect_token_verify(monkeypatch):
    env(monkeypatch)
    good = jwt.encode({"exp": int(time.time()) + 100, "iss": KEY}, SECRET.encode())
    with Capture() as c:
        assert cli.main(["inspect-token", good, "--verify"]) == 0
    assert "VERIFIED" in c.out.getvalue()
    for bad in (jwt.encode({"exp": int(time.time()) + 100, "iss": KEY}, b"z" * 40),
                jwt.encode({"exp": int(time.time()) - 5, "iss": KEY}, SECRET.encode()),
                jwt.encode({"exp": int(time.time()) + 100, "iss": "other"}, SECRET.encode()), "garbage"):  # fmt: skip
        with Capture() as c:
            assert cli.main(["inspect-token", bad, "--verify"]) == 1
        assert c.err.getvalue().startswith("error:")


def test_inspect_token_reports_an_expired_token(monkeypatch):
    token = jwt.encode({"exp": int(time.time()) - 30}, b"k" * 40)
    with Capture() as c:
        cli.main(["inspect-token", token])
    assert "expired" in c.out.getvalue()


def test_inspect_token_reads_stdin(monkeypatch):
    token = jwt.encode({"exp": int(time.time()) + 100, "sub": "piped"}, b"k" * 40)
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    with Capture() as c:
        assert cli.main(["inspect-token"]) == 0
    assert "piped" in c.out.getvalue()


def test_demo_is_deterministic_and_shows_the_main_features():
    outs = []
    for _ in range(2):
        with Capture() as c:
            assert cli.main(["demo"]) == 0
        outs.append(c.out.getvalue())
    assert outs[0] == outs[1]
    text = outs[0]
    for needle in ["role=technician", "role=observer", "hidden=True", "403 forbidden", "422 validation_error",
                   "409 session_exists", "rejected   wrong secret: bad signature", "token expired",
                   "10 tokens issued, then 429", "409 session_closed", "still valid for 480 s", "(same as above)"]:  # fmt: skip
        assert needle in text, needle
    assert cli.DEMO_SECRET not in text  # the demo prints tokens' claims, never the signing secret
    start = text.index('{\n    "iss"')
    claims = json.loads(text[start:text.index("\n}\n", start) + 2])
    assert claims["exp"] - claims["nbf"] == 605
