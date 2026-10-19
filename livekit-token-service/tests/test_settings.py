import pytest

from livekit_tokens.errors import ConfigError
from livekit_tokens.settings import Settings

GOOD = {"LIVEKIT_API_KEY": "plantkey", "LIVEKIT_API_SECRET": "k9Qw2Zr7Lm4Xv8Bn3Td6Hs1Jc5Fg0Yp2Aa9UeRt",
        "LIVEKIT_URL": "wss://media.example.test"}  # fmt: skip


def env(**over):
    e = dict(GOOD)
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


def test_good_configuration_and_defaults():
    s = Settings.from_env(env())
    assert (s.api_key, s.url, s.default_ttl, s.max_ttl, s.min_ttl, s.rate_per_minute) == ("plantkey", "wss://media.example.test", 600, 900, 30, 10)  # fmt: skip
    assert s.api_secret == GOOD["LIVEKIT_API_SECRET"].encode()
    assert s.session_seconds == 7200


def test_the_secret_is_not_in_repr():
    assert GOOD["LIVEKIT_API_SECRET"] not in repr(Settings.from_env(env()))


@pytest.mark.parametrize("name", ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_values(name, value):
    with pytest.raises(ConfigError):
        Settings.from_env(env(**{name: value}))


@pytest.mark.parametrize("secret", ["short", "x" * 31, "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1u", "change-me-" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1", "Secret" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1",
                                    "a" * 40, "ab" * 20, "devsecret" + "q" * 30])  # fmt: skip
def test_weak_or_placeholder_secrets_are_refused(secret):
    with pytest.raises(ConfigError):
        Settings.from_env(env(LIVEKIT_API_SECRET=secret))


def test_secret_of_exactly_the_minimum_length_is_fine():
    Settings.from_env(env(LIVEKIT_API_SECRET="aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1uW"))  # 32 chars


@pytest.mark.parametrize("key", ["a", "ab", "bad key", "bad/key", "k" * 65, "ключ"])
def test_bad_api_key(key):
    with pytest.raises(ConfigError):
        Settings.from_env(env(LIVEKIT_API_KEY=key))


@pytest.mark.parametrize("url", ["http://x", "https://x", "localhost:7880", "ftp://x"])
def test_url_needs_a_websocket_scheme(url):
    with pytest.raises(ConfigError):
        Settings.from_env(env(LIVEKIT_URL=url))


@pytest.mark.parametrize("name,bad", [("TOKEN_TTL_MAX", "abc"), ("TOKEN_TTL_MAX", "-5"), ("TOKEN_TTL_MAX", "99999"),
                                      ("RATE_PER_MINUTE", "0"), ("SESSION_SECONDS", "10"), ("TOKEN_TTL_MIN", "9")])  # fmt: skip
def test_numbers_are_validated(name, bad):
    with pytest.raises(ConfigError):
        Settings.from_env(env(**{name: bad}))


def test_ttl_ordering():
    with pytest.raises(ConfigError):
        Settings.from_env(env(TOKEN_TTL_DEFAULT="1000", TOKEN_TTL_MAX="900"))
    with pytest.raises(ConfigError):
        Settings.from_env(env(TOKEN_TTL_DEFAULT="20", TOKEN_TTL_MIN="30"))
    s = Settings.from_env(env(TOKEN_TTL_DEFAULT="900", TOKEN_TTL_MAX="900", TOKEN_TTL_MIN="900"))
    assert s.default_ttl == 900


@pytest.mark.parametrize("name,low,high,attr", [("TOKEN_TTL_MAX", 30, 3600, "max_ttl"), ("RATE_PER_MINUTE", 1, 600, "rate_per_minute"),
                                                ("SESSION_SECONDS", 60, 86400, "session_seconds")])  # fmt: skip
def test_number_bounds_are_inclusive(name, low, high, attr):
    for value in (low, high):
        extra = {"TOKEN_TTL_DEFAULT": "30"} if name == "TOKEN_TTL_MAX" else {}
        assert getattr(Settings.from_env(env(**{name: str(value)}, **extra)), attr) == value
    for value in (low - 1, high + 1):
        with pytest.raises(ConfigError):
            Settings.from_env(env(**{name: str(value)}))
