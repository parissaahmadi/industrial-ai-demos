"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate10.py       (RUNNER="uv run pytest -q -x" python mutate10.py to choose the test command)
A mutant that makes the suite hang counts as killed (per-mutant timeout, MUTATION_TIMEOUT seconds, default 90).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()
TIMEOUT = int(os.environ.get("MUTATION_TIMEOUT", "90"))
COPY = ["src", "tests", "deploy", "pyproject.toml", "compose.yaml", "Dockerfile", ".env.example", ".gitignore", "README.md"]
S = "src/livekit_tokens/"

# (file, [(old, new), ...], what the bug is)
MUTATIONS = [
    # --- jwt
    (S + "jwt.py", [('if header.get("alg") != "HS256":', "if False:")], "algorithm taken from the token header"),
    (S + "jwt.py", [("if not hmac.compare_digest(expected, b64url_decode(s)):", "if False:")], "signature not checked"),
    (S + "jwt.py", [("if now >= exp + leeway:", "if now > exp + leeway:")], "token valid at its exact expiry second"),
    (S + "jwt.py", [("if now >= exp + leeway:", "if now >= exp - leeway:")], "leeway applied the wrong way"),
    (S + "jwt.py", [("if nbf is not None and now < nbf - leeway:", "if nbf is not None and now <= nbf - leeway:")], "nbf boundary off by one"),
    (S + "jwt.py", [("if not isinstance(exp, int) or isinstance(exp, bool):", "if not isinstance(exp, int):")], "exp=true accepted"),
    (S + "jwt.py", [("if nbf is not None and (not isinstance(nbf, int) or isinstance(nbf, bool)):", "if False:")], "nbf type not checked"),
    (S + "jwt.py", [('if issuer is not None and claims.get("iss") != issuer:', "if False:")], "issuer not checked"),
    (S + "jwt.py", [("    if not secret:\n", "    if False:\n")], "empty signing secret accepted"),
    (S + "jwt.py", [("if not text or any(", "if any(")], "empty base64url segment accepted"),
    (S + "jwt.py", [("if len(parts) != 3:", "if len(parts) < 2:")], "unsigned (two-part) token accepted"),
    (S + "jwt.py", [('{"alg": "HS256", "typ": "JWT"}', '{"alg": "HS256"}')], "typ header dropped"),
    # --- grants
    (S + "grants.py", [('can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=True', 'can_publish=True, can_subscribe=True, can_publish_data=False, sources=(), hidden=True')], "observers may publish"),
    (S + "grants.py", [('can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=True', 'can_publish=False, can_subscribe=True, can_publish_data=True, sources=(), hidden=True')], "observers may send data"),
    (S + "grants.py", [('can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=True', 'can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=False')], "observers are visible"),
    (S + "grants.py", [('can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=True', 'can_publish=False, can_subscribe=False, can_publish_data=False, sources=(), hidden=True')], "observers cannot subscribe"),
    (S + "grants.py", [("sources=(CAMERA, MICROPHONE), hidden=False)", "sources=(CAMERA, MICROPHONE, SCREEN_SHARE), hidden=False)")], "technicians may share a screen"),
    (S + "grants.py", [("sources=(MICROPHONE, CAMERA, SCREEN_SHARE), hidden=False)", "sources=(MICROPHONE, CAMERA), hidden=False)")], "experts cannot share a screen"),
    (S + "grants.py", [("        if self.can_publish:\n", "        if True:\n")], "empty source list emitted for observers"),
    (S + "grants.py", [('"roomJoin": True,', '"roomJoin": True,\n            "roomAdmin": True,')], "room admin grant added"),
    (S + "grants.py", [('"roomJoin": True,', '"roomJoin": False,')], "join not granted"),
    (S + "grants.py", [("if participant_role not in _RULES:", "if False:")], "unknown role not rejected explicitly"),
    # --- settings
    (S + "settings.py", [("if len(secret) < MIN_SECRET_CHARS:", "if len(secret) <= MIN_SECRET_CHARS:")], "secret length boundary off by one"),
    (S + "settings.py", [("MIN_SECRET_CHARS = 32", "MIN_SECRET_CHARS = 16")], "short secrets accepted"),
    (S + "settings.py", [(" or len(set(secret)) < 8", "")], "repetitive secret accepted"),
    (S + "settings.py", [("secret.lower().startswith(PLACEHOLDER_PREFIXES)", "secret.startswith(PLACEHOLDER_PREFIXES)")], "placeholder check is case sensitive"),
    (S + "settings.py", [('if not url.startswith(("ws://", "wss://")):', "if False:")], "URL scheme not checked"),
    (S + "settings.py", [('r"[A-Za-z0-9_-]{3,64}"', 'r"[A-Za-z0-9_-]{1,64}"')], "one-letter API key accepted"),
    (S + "settings.py", [("if not s.min_ttl <= s.default_ttl <= s.max_ttl:", "if False:")], "ttl ordering not checked"),
    (S + "settings.py", [("    api_secret: bytes = field(repr=False)", "    api_secret: bytes")], "secret printed in repr"),
    (S + "settings.py", [("not low <= int(raw) <= high", "not low <= int(raw) < high")], "number bound off by one"),
    (S + "settings.py", [('"TOKEN_TTL_DEFAULT", 600,', '"TOKEN_TTL_DEFAULT", 300,')], "default ttl changed"),
    (S + "settings.py", [('"TOKEN_TTL_MAX", 900,', '"TOKEN_TTL_MAX", 1800,')], "max ttl doubled"),
    (S + "settings.py", [('"SESSION_SECONDS", 2 * 3600,', '"SESSION_SECONDS", 24 * 3600,')], "session length changed"),
    # --- rate limit
    (S + "ratelimit.py", [("hits[0] <= now - self.window", "hits[0] < now - self.window")], "window boundary off by one"),
    (S + "ratelimit.py", [("if len(hits) >= self.limit:", "if len(hits) > self.limit:")], "limit off by one"),
    (S + "ratelimit.py", [("math.ceil(hits[0] + self.window - now)", "math.floor(hits[0] + self.window - now)")], "retry-after rounded down"),
    (S + "ratelimit.py", [("wait = max(1,", "hits.append(now)\n                wait = max(1,")], "refused requests extend the block"),
    (S + "ratelimit.py", [("if len(self._hits) > 10_000:", "if False:")], "idle keys never pruned"),
    # --- token
    (S + "token.py", [("NBF_BACKDATE = 5", "NBF_BACKDATE = 0")], "no clock-skew allowance"),
    (S + "token.py", [('"iss": settings.api_key,', '"iss": "x",')], "wrong issuer"),
    (S + "token.py", [('"sub": identity,', '"sub": name,')], "identity is the display name"),
    # --- service rules
    (S + "service.py", [('        if p.role != "technician":\n            raise self._deny(p, "session.create"', '        if False:\n            raise self._deny(p, "session.create"')], "anyone may open a session"),
    (S + "service.py", [('if p.role != "admin" and row["created_by"] != p.username:', 'if row["created_by"] != p.username:')], "admins cannot close sessions"),
    (S + "service.py", [('if p.role != "admin" and row["created_by"] != p.username:', "if False:")], "anyone may close any session"),
    (S + "service.py", [('"SELECT id FROM sessions WHERE created_by = ? AND closed_at IS NULL AND expires_at > ?",', '"SELECT id FROM sessions WHERE created_by = ? AND expires_at > ?",')], "closed sessions still block a new one"),
    (S + "service.py", [('"SELECT id FROM sessions WHERE created_by = ? AND closed_at IS NULL AND expires_at > ?",', '"SELECT id FROM sessions WHERE created_by = ? AND closed_at IS NULL",')], "expired sessions still block a new one"),
    (S + "service.py", [('if busy:', "if False:")], "several open sessions per technician"),
    (S + "service.py", [('"expired" if now >= r["expires_at"]', '"expired" if now > r["expires_at"]')], "session open at its exact end"),
    (S + "service.py", [('state = "closed" if r["closed_at"] is not None else "expired" if now >= r["expires_at"] else "open"', 'state = "expired" if now >= r["expires_at"] else "closed" if r["closed_at"] is not None else "open"')], "expired wins over closed"),
    (S + "service.py", [('return p.role != "technician" or row["created_by"] == p.username', "return True")], "technicians see each other's sessions"),
    (S + "service.py", [('return "technician" if row["created_by"] == p.username else None', 'return "technician"')], "any technician may join any session"),
    (S + "service.py", [('"supervisor": "observer"', '"supervisor": "expert"')], "supervisors join as experts"),
    (S + "service.py", [('"admin": "observer"', '"admin": "expert"')], "admins join as experts"),
    (S + "service.py", [('{"expert": "expert",', '{"expert": "observer",')], "experts join as observers"),
    (S + "service.py", [('exp = min(now + ttl, row["expires_at"])', "exp = now + ttl")], "token outlives the session"),
    (S + "service.py", [("not s.min_ttl <= ttl <= s.max_ttl", "not s.min_ttl <= ttl < s.max_ttl")], "max ttl excluded"),
    (S + "service.py", [("not s.min_ttl <= ttl <= s.max_ttl", "not s.min_ttl < ttl <= s.max_ttl")], "min ttl excluded"),
    (S + "service.py", [("isinstance(ttl, bool) or ", "")], "ttl=true accepted"),
    (S + "service.py", [("self._limiter.hit(p.username, self._now())", "pass")], "no rate limit"),
    (S + "service.py", [("self._limiter.hit(p.username, self._now())", 'self._limiter.hit("all", self._now())')], "one rate limit for everybody"),
    (S + "service.py", [('"expires_in": exp - now', '"expires_in": ttl')], "expires_in ignores the session end"),
    (S + "service.py", [("hashlib.sha256(token.encode()).hexdigest()[:16]", "token[:16]")], "audit stores a token prefix"),
    (S + "service.py", [("fingerprint=fingerprint)", "fingerprint=fingerprint, token=token)")], "audit stores the whole token"),
    (S + "service.py", [('self._audit(p.username, "access.denied", target, attempted=action, why=why)', "pass")], "denials not audited"),
    (S + "service.py", [('        if p.role != "admin":\n            raise self._deny(p, "audit.read"', '        if False:\n            raise self._deny(p, "audit.read"')], "anyone may read the audit trail"),
    (S + "service.py", [("if not 1 <= limit <= 200:", "if not 1 <= limit <= 199:")], "audit page limit off by one"),
    (S + "service.py", [("asset_id, reason = asset_id.strip(), reason.strip()", "pass")], "input not trimmed"),
    (S + "service.py", [(" or _CONTROL.search(reason)", "")], "control characters in reason"),
    (S + "service.py", [("not 1 <= len(reason) <= 200", "not 1 <= len(reason) < 200")], "reason length boundary"),
    (S + "service.py", [("not ASSET_RE.fullmatch(asset_id)", "not ASSET_RE.match(asset_id)")], "asset id only checked as a prefix"),
    (S + "service.py", [('if view["state"] == "closed":', "if False:")], "tokens for closed sessions"),
    (S + "service.py", [('if view["state"] == "expired":', "if False:")], "tokens for expired sessions"),
    (S + "service.py", [("(db.hash_token(token), username, self.now())", "(token, username, self.now())")], "API tokens stored in clear"),
    (S + "service.py", [('            if row["closed_at"] is None:\n                self.db.execute("UPDATE', '            if True:\n                self.db.execute("UPDATE')], "closing again overwrites the closer"),
    (S + "service.py", [("secrets.token_hex(8)", "secrets.token_hex(2)")], "guessable room names"),
    (S + "service.py", [('self._audit(p.username, "token.rate_limited", target)', "pass")], "rate limiting not audited"),
    # --- http
    (S + "api.py", [('"cache-control": "no-store", ', "")], "responses cacheable"),
    (S + "api.py", [("MAX_BODY = 4 * 1024", "MAX_BODY = 64 * 1024")], "body limit raised"),
    (S + "api.py", [("if declared and declared.isdigit() and int(declared) > MAX_BODY:", "if False:")], "declared length not checked up front"),
    (S + "api.py", [("        if total > MAX_BODY:", "        if False:")], "streamed (chunked) bodies not limited"),
    (S + "api.py", [("""headers["www-authenticate"] = 'Bearer realm="livekit-tokens"'""", "pass")], "401 without WWW-Authenticate"),
    (S + "api.py", [('headers["retry-after"] = str(exc.extra["retry_after"])', "pass")], "429 without Retry-After"),
    (S + "api.py", [('problem(500, "internal_error", "something went wrong on the server")', 'problem(500, "internal_error", repr(exc))')], "internal errors leak details"),
    (S + "api.py", [("""{"location": f"/sessions/{s['id']}"}""", "{}")], "201 without Location"),
    (S + "models.py", [('    model_config = ConfigDict(extra="forbid")\n    ttl_seconds', "    ttl_seconds")], "unknown token request fields ignored"),
    (S + "models.py", [("ttl_seconds: StrictInt | None", "ttl_seconds: int | None")], "ttl accepted as a string"),
    # --- cli
    (S + "cli.py", [("        if args.verify:\n", "        if False:\n")], "--verify does nothing"),
    (S + "cli.py", [('print(f"configuration error: {e}", file=sys.stderr)\n        return 2', 'print(f"configuration error: {e}", file=sys.stderr)\n        return 1')], "config error exit code"),
    # --- deployment files
    ("compose.yaml", [('"127.0.0.1:8000:8000"', '"8000:8000"')], "token API published on all interfaces"),
    ("compose.yaml", [('LIVEKIT_API_SECRET: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET in .env}"', 'LIVEKIT_API_SECRET: "${LIVEKIT_API_SECRET}"')], "empty secret allowed for the token service"),
    ("compose.yaml", [("}: ${LIVEKIT_API_SECRET:?", "}: ${LIVEKIT_API_KEY:?")], "media server gets the key as its secret"),
    ("compose.yaml", [('"7882:7882/udp"', '"7882:7882"')], "UDP port published as TCP"),
    ("compose.yaml", [("token-data:/data", "token-data:/var/lib/data")], "database not on the volume"),
    ("compose.yaml", [("127.0.0.1:8000/health", "127.0.0.1:9000/health")], "healthcheck on the wrong port"),
    ("deploy/livekit.yaml", [("udp_port: 7882", "udp_port: 7883")], "UDP port differs from compose"),
    ("deploy/livekit.yaml", [("logging:", "keys:\n  plantkey: abc\nlogging:")], "key pair written into the server config"),
    ("Dockerfile", [("USER app\n", "")], "container runs as root"),
    ("Dockerfile", [('"--port", "8000"', '"--port", "8080"')], "container port differs from compose"),
    (".env.example", [("change-me-generate-with-openssl-rand-hex-32", "k9Qw2Zr7Lm4Xv8Bn3Td6Hs1Jc5Fg0Yp2Aa9UeRt")], "example env carries a usable-looking secret"),
    (".gitignore", [("dist/\n.env\n", "dist/\n")], ".env not ignored"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return 124


def main() -> int:
    survived = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (rel, edits, why) in enumerate(MUTATIONS, 1):
            tree = Path(tmp) / f"m{i}"
            for name in COPY:
                src = ROOT / name
                if src.is_dir():
                    shutil.copytree(src, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    tree.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, tree / name)
            target = tree / rel
            text = target.read_text()
            for old, new in edits:
                if old not in text:
                    print(f"{i:2} SETUP ERROR  pattern not found in {rel}: {old[:70]!r}")
                    return 2
                text = text.replace(old, new, 1)
            target.write_text(text)
            killed = run(tree) != 0
            print(f"{i:2} {'killed  ' if killed else 'SURVIVED'} {Path(rel).name:14} {why}", flush=True)
            if not killed:
                survived.append(why)
            shutil.rmtree(tree, ignore_errors=True)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
