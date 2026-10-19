import base64
import hashlib
import hmac
import json

import pytest

from livekit_tokens import jwt

# RFC 7515 appendix A.1: the published HS256 example, so the signer is checked against someone else's answer.
RFC_TOKEN = (
    "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9."
    "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ."
    "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
RFC_KEY = base64.urlsafe_b64decode(
    "AyM1SysPpbyDfgZld3umj1qzKObwVMkoqQ-EstJQLr_T-1qS0gZH75aKtMN3Yj0iPS4hcgUuTwjAzZr1Z9CAow==")
SECRET = b"s" * 40


def forge(header: dict, claims: dict, secret: bytes = SECRET, sig: bytes | None = None) -> str:
    h = jwt.b64url(json.dumps(header).encode())
    p = jwt.b64url(json.dumps(claims).encode())
    s = sig if sig is not None else hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{jwt.b64url(s)}"


def test_rfc7515_vector_is_accepted_and_the_signature_matches():
    h, p, s = RFC_TOKEN.split(".")
    expected = hmac.new(RFC_KEY, f"{h}.{p}".encode(), hashlib.sha256).digest()
    assert jwt.b64url(expected) == s  # the published signature is what HMAC-SHA256 gives
    claims = jwt.decode(RFC_TOKEN, RFC_KEY, now=1300819379)
    assert claims["iss"] == "joe" and claims["exp"] == 1300819380


def test_encode_signs_the_same_way_the_rfc_does():
    token = jwt.encode({"a": 1, "exp": 10}, SECRET)
    h, p, s = token.split(".")
    assert json.loads(jwt.b64url_decode(h)) == {"alg": "HS256", "typ": "JWT"}
    assert jwt.b64url(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest()) == s
    assert "=" not in token and "+" not in token and "/" not in token


def test_roundtrip_keeps_unicode():
    claims = {"exp": 100, "name": "تکنسین الف", "video": {"roomJoin": True}}
    assert jwt.decode(jwt.encode(claims, SECRET), SECRET, now=0) == claims


def test_empty_secret_is_refused_when_signing():
    with pytest.raises(ValueError):
        jwt.encode({"exp": 1}, b"")


@pytest.mark.parametrize("now,ok", [(99, True), (100, False), (101, False)])
def test_expiry_boundary(now, ok):
    t = jwt.encode({"exp": 100}, SECRET)
    if ok:
        jwt.decode(t, SECRET, now=now)
    else:
        with pytest.raises(jwt.ExpiredToken):
            jwt.decode(t, SECRET, now=now)


@pytest.mark.parametrize("now,ok", [(49, False), (50, True), (51, True)])
def test_not_before_boundary(now, ok):
    t = jwt.encode({"exp": 100, "nbf": 50}, SECRET)
    if ok:
        jwt.decode(t, SECRET, now=now)
    else:
        with pytest.raises(jwt.NotYetValid):
            jwt.decode(t, SECRET, now=now)


def test_leeway_applies_to_both_ends():
    t = jwt.encode({"exp": 100, "nbf": 50}, SECRET)
    jwt.decode(t, SECRET, now=104, leeway=5)
    jwt.decode(t, SECRET, now=46, leeway=5)
    with pytest.raises(jwt.ExpiredToken):
        jwt.decode(t, SECRET, now=105, leeway=5)
    with pytest.raises(jwt.NotYetValid):
        jwt.decode(t, SECRET, now=44, leeway=5)


def test_wrong_secret_and_tampered_payload():
    t = jwt.encode({"exp": 100, "sub": "a"}, SECRET)
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(t, b"t" * 40, now=0)
    h, _, s = t.split(".")
    evil = f"{h}.{jwt.b64url(json.dumps({'exp': 100, 'sub': 'root'}).encode())}.{s}"
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(evil, SECRET, now=0)


@pytest.mark.parametrize("alg", ["none", "None", "HS384", "HS512", "RS256", "", None])
def test_other_algorithms_are_refused_even_with_a_valid_looking_signature(alg):
    t = forge({"alg": alg, "typ": "JWT"}, {"exp": 100})
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(t, SECRET, now=0)


def test_unsigned_token_is_refused():
    h = jwt.b64url(b'{"alg":"none"}')
    p = jwt.b64url(b'{"exp":100}')
    for t in (f"{h}.{p}.", f"{h}.{p}"):
        with pytest.raises(jwt.InvalidToken):
            jwt.decode(t, SECRET, now=0)


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "...", "!!!.???.***", None, 5, "e30.e30.%%%"])
def test_garbage_is_invalid_not_a_crash(bad):
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(bad, SECRET, now=0)


def test_payload_must_be_a_json_object():
    for payload in (b"[1]", b'"x"', b"123", b"null", b"\xff\xfe"):
        h = jwt.b64url(b'{"alg":"HS256"}')
        p = jwt.b64url(payload)
        sig = hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest()
        with pytest.raises(jwt.InvalidToken):
            jwt.decode(f"{h}.{p}.{jwt.b64url(sig)}", SECRET, now=0)


@pytest.mark.parametrize("claims", [{}, {"exp": "100"}, {"exp": 100.5}, {"exp": True}, {"exp": None}])
def test_exp_is_required_and_must_be_an_integer(claims):
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(jwt.encode(claims, SECRET), SECRET, now=0)


@pytest.mark.parametrize("nbf", ["1", 1.5, True])
def test_nbf_must_be_an_integer_when_present(nbf):
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(jwt.encode({"exp": 100, "nbf": nbf}, SECRET), SECRET, now=50)


def test_issuer_check():
    t = jwt.encode({"exp": 100, "iss": "plantkey"}, SECRET)
    assert jwt.decode(t, SECRET, now=0, issuer="plantkey")["iss"] == "plantkey"
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(t, SECRET, now=0, issuer="other")
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(jwt.encode({"exp": 100}, SECRET), SECRET, now=0, issuer="plantkey")
    jwt.decode(t, SECRET, now=0)  # no issuer given: not checked


def test_unverified_claims_ignores_the_signature_by_design():
    t = forge({"alg": "HS256"}, {"exp": 5, "sub": "x"}, sig=b"nonsense")
    assert jwt.unverified_claims(t)["sub"] == "x"
    with pytest.raises(jwt.InvalidToken):
        jwt.unverified_claims("nope")


def test_b64url_decode_rejects_padding_and_standard_alphabet():
    for bad in ("YQ==", "a+b", "a/b", "a b", ""):
        with pytest.raises(jwt.InvalidToken):
            jwt.b64url_decode(bad)
    assert jwt.b64url_decode("YQ") == b"a"
