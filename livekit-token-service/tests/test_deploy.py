"""The deployment files are code too: check that they agree with each other and with the service."""

from pathlib import Path

import pytest
import yaml

from livekit_tokens.errors import ConfigError
from livekit_tokens.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_both_services_use_the_same_key_pair_from_env():
    lk = COMPOSE["services"]["livekit"]["environment"]["LIVEKIT_KEYS"]
    ts = COMPOSE["services"]["token-service"]["environment"]
    assert "${LIVEKIT_API_KEY" in lk and "${LIVEKIT_API_SECRET" in lk
    assert "${LIVEKIT_API_KEY" in ts["LIVEKIT_API_KEY"] and "${LIVEKIT_API_SECRET" in ts["LIVEKIT_API_SECRET"]


def test_no_secret_value_is_written_in_any_deploy_file():
    for path in (ROOT / "compose.yaml", ROOT / "deploy" / "livekit.yaml", ROOT / "Dockerfile"):
        text = path.read_text()
        assert "keys:" not in text.lower().replace("livekit_keys", "")
        assert "secret:" not in text.lower().replace("api_secret:", "")  # no `secret: value` lines


def test_required_variables_fail_loudly_in_compose():
    """An unset key or secret must stop `docker compose up`, not start a server with an empty secret."""
    env = COMPOSE["services"]["token-service"]["environment"]
    for var in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        assert "${" + var + ":?" in env[var]
        assert "${" + var + ":?" in COMPOSE["services"]["livekit"]["environment"]["LIVEKIT_KEYS"]


def test_example_env_is_refused_by_the_service_until_the_secret_is_replaced():
    env = dict(line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines()
               if line and not line.startswith("#"))  # fmt: skip
    with pytest.raises(ConfigError):
        Settings.from_env(env)
    env["LIVEKIT_API_SECRET"] = "k9Qw2Zr7Lm4Xv8Bn3Td6Hs1Jc5Fg0Yp2Aa9UeRt"
    assert Settings.from_env(env).url == "ws://localhost:7880"


def test_dotenv_is_git_ignored_and_example_is_not():
    ignored = (ROOT / ".gitignore").read_text().split()
    assert ".env" in ignored and ".env.example" not in ignored


def test_livekit_ports_match_between_config_and_compose():
    cfg = yaml.safe_load((ROOT / "deploy" / "livekit.yaml").read_text())
    published = [str(p) for p in COMPOSE["services"]["livekit"]["ports"]]
    assert f"{cfg['port']}:{cfg['port']}" in published
    assert f"{cfg['rtc']['tcp_port']}:{cfg['rtc']['tcp_port']}" in published
    assert f"{cfg['rtc']['udp_port']}:{cfg['rtc']['udp_port']}/udp" in published
    assert "keys" not in cfg


def test_the_token_api_listens_on_loopback_only_by_default():
    assert COMPOSE["services"]["token-service"]["ports"] == ["127.0.0.1:8000:8000"]


def test_default_livekit_url_points_at_the_published_signalling_port():
    default = COMPOSE["services"]["token-service"]["environment"]["LIVEKIT_URL"]
    assert default.endswith(":-ws://localhost:7880}")


def test_dockerfile_runs_unprivileged_and_serves_on_the_compose_port():
    text = (ROOT / "Dockerfile").read_text()
    lines = [x.strip() for x in text.splitlines()]
    assert "USER app" in lines and lines.index("USER app") < next(i for i, x in enumerate(lines) if x.startswith("CMD"))
    assert '"--port", "8000"' in text and "/data/tokens.sqlite3" in text
    mount = COMPOSE["services"]["token-service"]["volumes"][0]
    assert mount.startswith("token-data:/data")
    for copied in ("pyproject.toml", "README.md", "src"):
        assert (ROOT / copied).exists() and copied in text  # every COPY source exists in the build context


def test_healthcheck_targets_the_service_port():
    hc = COMPOSE["services"]["token-service"]["healthcheck"]["test"]
    assert "127.0.0.1:8000/health" in " ".join(hc)
