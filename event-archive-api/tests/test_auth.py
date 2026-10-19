import pytest

from event_archive import db
from event_archive.errors import NotFound, Unauthorized
from helpers import TOKENS, audit_actions, make_archive


def test_valid_token_gives_principal():
    a = make_archive()
    p = a.authenticate(TOKENS["adm.d"])
    assert (p.username, p.role) == ("adm.d", "admin")


@pytest.mark.parametrize("token", [None, "", "nope", "test-token-adm.d-0000000001"])
def test_bad_tokens(token):
    with pytest.raises(Unauthorized):
        make_archive().authenticate(token)


def test_revoked_token_and_inactive_user_are_refused():
    a = make_archive()
    a.db.execute("UPDATE api_tokens SET revoked = 1 WHERE username = 'op.b'")
    with pytest.raises(Unauthorized):
        a.authenticate(TOKENS["op.b"])
    a.db.execute("UPDATE users SET active = 0 WHERE username = 'op.c'")
    with pytest.raises(Unauthorized):
        a.authenticate(TOKENS["op.c"])
    assert a.authenticate(TOKENS["view.a"]).role == "viewer"


def test_issued_token_works_and_only_its_hash_is_stored():
    a = make_archive()
    token = a.issue_token("op.b")
    assert a.authenticate(token).username == "op.b"
    stored = [r[0] for r in a.db.execute("SELECT token_sha256 FROM api_tokens")]
    assert token not in stored and db.hash_token(token) in stored
    assert audit_actions(a) == ["token.issue"]


def test_issuing_for_unknown_user_fails_cleanly():
    a = make_archive()
    with pytest.raises(NotFound):
        a.issue_token("ghost")
    assert audit_actions(a) == []


def test_tokens_are_random():
    a = make_archive()
    assert a.issue_token("op.b") != a.issue_token("op.b")
