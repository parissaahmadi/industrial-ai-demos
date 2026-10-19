# livekit-token-service

A small service that issues **short-lived [LiveKit](https://livekit.io) access tokens** so a field technician can
get **remote help** from an expert while a supervisor looks in. It decides *who may join which room, as what, and for
how long*; the media server does the video.

> **Demo data is fictional.** Assets (`PMP-0001`, `FAN-0003`), people (`tech.a`, `exp.c`, ...), the brand key
> (`plantkey`) and the demo signing secret are invented. Nothing comes from a real site, company or deployment.

What it shows (each item is covered by tests, see [Testing](#testing)):

| Topic | What is implemented |
|---|---|
| Tokens | HS256 JWT written with the standard library (`jwt.py`), checked against the published RFC 7515 test vector; `iss`/`sub`/`name`/`nbf`/`exp`/`video`/`metadata`/`attributes` claims |
| Grants | One allow-list (`grants.py`): *technician* (camera + mic + data), *expert* (mic + camera + screen share + data), *observer* (listen only, **hidden**, cannot publish). Admin-level grants (`roomAdmin`, `roomCreate`, `roomList`, `roomRecord`, `ingressAdmin`) are never emitted; a test enforces it |
| Sessions | A technician opens a session for an asset (one open session each); the room name is random (`assist-` + 16 hex); a session can be joined for 2 h at most, or until it is closed |
| Lifetime | Default 10 min, allowed 30-900 s; a token never outlives its session; `nbf` is back-dated 5 s for clock differences |
| Who joins as what | The owner joins as technician, any expert as expert, supervisors and admins as observers, other technicians not at all |
| Abuse limits | 10 tokens per user per minute (sliding window, `429` + `Retry-After`), 4 KiB bodies (declared and streamed), strict JSON bodies (unknown fields rejected) |
| Audit | Append-only (SQLite triggers). Records session open/close, every issued token as a **SHA-256 fingerprint** (never the token), denials and rate limiting. Admin-only to read |
| Config | Environment variables; refuses to start on a missing, short (< 32 chars), repetitive or placeholder secret |
| Deployment | `compose.yaml` (LiveKit server + this service), `Dockerfile` (non-root), `.env.example`; consistency between those files is tested |

## What a token can and cannot do (read this first)

These come from LiveKit's own documentation and shape the design:

* **`exp` only limits the initial connection.** A participant who is already connected stays connected, and
  reconnects are not blocked by it.
* **A self-hosted LiveKit server cannot revoke tokens.** Closing a session here stops *new* tokens; a token issued a
  minute ago still works until it expires. That is why tokens are short (≤ 15 min) and why they are capped at the
  session end. Removing someone who is already in the room needs LiveKit's RoomService API (`RemoveParticipant`),
  which this service does **not** call (see [Limits](#design-decisions-and-limits-read-before-reusing)).
* The `video` grant is the whole authorisation model of the media server, so `grants.py` is the file to review.

## Run it

```bash
uv sync
uv run livekit-tokens demo                                   # scripted, offline, fixed clock (output below)

cp .env.example .env                                         # then put a real secret in it:
#   LIVEKIT_API_SECRET=$(openssl rand -hex 32)
set -a; . ./.env; set +a
uv run livekit-tokens init-db --db tokens.sqlite3            # prints one API token per fictional user, once
uv run livekit-tokens serve --db tokens.sqlite3              # API on 127.0.0.1:8000
uv run livekit-tokens inspect-token <jwt> --verify           # decode a LiveKit token (and check its signature)

docker compose up --build                                    # LiveKit + token service (see "not verified")
docker compose run --rm token-service livekit-tokens init-db --db /data/tokens.sqlite3
```

## The scripted demo (real output)

`uv run livekit-tokens demo` (`python -m livekit_tokens demo`). Output of this repository's code, unedited (the
clock and room names are fixed, so it is the same every run):

```text


== 1. a technician asks for remote help on a fictional pump
{
  "id": 1,
  "room": "assist-52e6b438",
  "asset_id": "PMP-0001",
  "reason": "Bearing noise after restart, need a second pair of eyes",
  "state": "open",
  "created_by": "tech.a",
  "created_at": "2026-10-12T08:00:00Z",
  "expires_at": "2026-10-12T10:00:00Z",
  "closed_at": null,
  "closed_by": null
}

== 2. tokens: same room, different grants
  tech.a  role=technician expires_in=600s publish=True  sources=['camera', 'microphone'] data=True  hidden=False
  exp.c   role=expert     expires_in=600s publish=True  sources=['microphone', 'camera', 'screen_share'] data=True  hidden=False
  sup.d   role=observer   expires_in=600s publish=False sources=[] data=False hidden=True
  claims of the technician's token:
{
    "iss": "plantkey",
    "sub": "tech.a",
    "name": "Technician A",
    "nbf": 1791791995,
    "exp": 1791792600,
    "video": {
        "roomJoin": true,
        "room": "assist-52e6b438",
        "canPublish": true,
        "canSubscribe": true,
        "canPublishData": true,
        "hidden": false,
        "canPublishSources": [
            "camera",
            "microphone"
        ]
    },
    "metadata": "{\"role\":\"technician\",\"asset\":\"PMP-0001\"}",
    "attributes": {
        "role": "technician"
    }
}

== 3. who is refused
  403 forbidden         another technician asks for a token: this is another technician's session
  403 forbidden         an expert tries to open a session: only a technician can ask for remote help
  422 validation_error  a ttl of 1 hour: ttl_seconds must be an integer from 30 to 900
  409 session_exists    the technician opens a second session: you already have an open session (1); close it first

== 4. a token is only as good as its signature and clock
  accepted   right secret, now
  rejected   wrong secret: bad signature
  rejected   11 minutes later (ttl was 10): token expired

== 5. rate limit: 10 tokens per user per minute
  10 tokens issued, then 429: at most 10 tokens per 60 s; try again in 60 s

== 6. closing the session
  new token: 409 session_closed
  but the expert's earlier token is still valid for 480 s more.
  (a self-hosted LiveKit server cannot revoke tokens: that is why they are short)

== 7. audit trail (admin)
  #1  tech.a  session.create     session:1  {"asset": "PMP-0001"}
  #2  tech.a  token.issue        session:1  {"exp": 1791792600, "fingerprint": "39b623ec782da3a1", "role": "technician"}
  #3  exp.c   token.issue        session:1  {"exp": 1791792600, "fingerprint": "1f06caf92077adcc", "role": "expert"}
  #4  sup.d   token.issue        session:1  {"exp": 1791792600, "fingerprint": "7282707d05e6305f", "role": "observer"}
  #5  tech.b  access.denied      session:1  {"attempted": "token.issue", "why": "this is another technician's session"}
  #6  exp.c   access.denied      sessions   {"attempted": "session.create", "why": "only a technician can ask for remote help"}
  #7  exp.c   token.issue        session:1  {"exp": 1791792060, "fingerprint": "d09e238068d1222e", "role": "expert"}
  #8  (same as above)
  #9  (same as above)
  #10 (same as above)
  #11 (same as above)
  #12 (same as above)
  #13 (same as above)
  #14 (same as above)
  #15 (same as above)
  #16 exp.c   token.rate_limited session:1  {}
  #17 tech.a  session.close      session:1  {}
  #18 tech.a  token.refused      session:1  {"why": "closed"}
```

Reading it:

* Step 2: one room, three different grants. The supervisor's token is `hidden` and cannot publish anything.
* Step 3: the refusals are `403`/`409`/`422`, not silent failures; the TTL cap is enforced on the server.
* Step 4: the signature and the clock are what make a token worth anything; the token is rejected under a wrong
  secret and after its expiry.
* Step 6: closing the session blocks **new** tokens only. The expert's earlier token is still valid for 480 s. This is
  the LiveKit limitation above, shown on purpose.
* Step 7: tokens appear only as fingerprints.

## Over HTTP (real session)

Real session against `serve` on this repository's code (API tokens and JWTs shortened; the clock is the real one):

```text
$ curl -H "Authorization: Bearer $TECH" -H "Content-Type: application/json" localhost:8765/sessions \
       -d '{"asset_id":"PMP-0001","reason":"Bearing noise after restart"}'
HTTP/1.1 201 Created
cache-control: no-store
location: /sessions/1
{"id":1,"room":"assist-8a7d6e5a4fe59add","asset_id":"PMP-0001","reason":"Bearing noise after restart","state":"open",
 "created_by":"tech.a","created_at":"2026-10-17T15:11:41Z","expires_at":"2026-10-17T17:11:41Z","closed_at":null,"closed_by":null}

$ curl -X POST -H "Authorization: Bearer $EXPERT" -H "Content-Type: application/json" \
       localhost:8765/sessions/1/token -d '{"ttl_seconds":120}'
{"token": "eyJhbGciOiJIUzI1NiIsInR5…gPIvxI", "url": "ws://localhost:7880", "room": "assist-8a7d6e5a4fe59add",
 "identity": "exp.c", "role": "expert", "expires_at": "2026-10-17T15:13:41Z", "expires_in": 120}

$ livekit-tokens inspect-token "$JWT" --verify
signature VERIFIED
{ "iss": "plantkey", "sub": "exp.c", "name": "Expert C", "nbf": 1792249896, "exp": 1792250021,
  "video": { "roomJoin": true, "room": "assist-8a7d6e5a4fe59add", "canPublish": true, "canSubscribe": true,
             "canPublishData": true, "hidden": false, "canPublishSources": ["microphone", "camera", "screen_share"] },
  "metadata": "{\"role\":\"expert\",\"asset\":\"PMP-0001\"}", "attributes": { "role": "expert" } }
expires in 120 s

$ curl -i -X POST -H "Authorization: Bearer $OTHER_TECHNICIAN" localhost:8765/sessions/1/token
HTTP/1.1 403 Forbidden
{"type":"forbidden","title":"forbidden","status":403,"detail":"this is another technician's session"}

$ curl -H "Authorization: Bearer $ADMIN" localhost:8765/audit        (one line per entry)
1 tech.a session.create session:1 {"asset": "PMP-0001"}
2 exp.c token.issue session:1 {"exp": 1792250021, "fingerprint": "de7f786241c6e0ca", "role": "expert"}
3 tech.b access.denied session:1 {"attempted": "token.issue", "why": "this is another technician's session"}
```

`nbf` is 5 s before issue (clock allowance), so `exp - nbf` is 125 s for a 120 s token.

On the client side, the app calls `POST /sessions/{id}/token`, then connects with the returned `url` and `token`
using a LiveKit client SDK (not included here).

## API

| Method and path | Roles | Notes |
|---|---|---|
| `GET /health` | none | |
| `GET /me` | any token | |
| `POST /sessions` | technician | `{"asset_id", "reason"}` → `201`; `409 session_exists` if one is already open |
| `GET /sessions` | any token | technician: own (last 20); others: all open sessions |
| `GET /sessions/{id}` | owner, expert, supervisor, admin | other technicians `403` |
| `POST /sessions/{id}/token` | owner, expert, supervisor, admin | optional `{"ttl_seconds": 30..900}`; `409 session_closed` / `session_expired`; `429` |
| `POST /sessions/{id}/close` | owner, admin | idempotent |
| `GET /audit` | admin | `after_id`, `limit` (1-200) |

Errors are RFC 9457-shaped: `{"type", "title", "status", "detail"}`. Every response is `Cache-Control: no-store`
(responses carry credentials). Unexpected exceptions are logged server-side and answered with a generic `500`.

## Design decisions and limits (read before reusing)

* **Not wired to LiveKit's server API.** There is no `RemoveParticipant`, no room creation (by default LiveKit creates a room
  on first join), no webhooks. Closing a session here does not eject anyone who is already connected.
* **No media, no recording.** `roomRecord` is never granted. Whether recording is allowed for remote help is a
  policy decision this demo does not take.
* **Identity = username.** Joining twice with the same identity makes LiveKit drop the first connection; that is
  accepted here (one device per person per room; this is LiveKit's behaviour as I know it, not tested here).
* **Rate limiting is in memory and per process.** It resets on restart and is not shared between replicas.
* **One writer.** A single SQLite connection behind a lock; sessions are created inside `BEGIN IMMEDIATE`. It is a
  deliberately small design, not a horizontally scaled one.
* **Audit** is append-only against the application, not against someone with raw database access (no hash chain here;
  the chained version is in `event-archive-api`). It records *that* a token was issued (role, expiry, fingerprint),
  not the reason text or the token.
* **API tokens** for the fictional users are random, stored as SHA-256, issued by the CLI only. There is no expiry,
  rotation or login flow; in production this would sit behind your real identity provider.
* **Clock.** JWT validity uses the service's clock; keep it NTP-synced. `nbf` is back-dated 5 s.
* **The responsibility for the secret** is yours: it lives in `.env` (git-ignored), is passed to both containers, and
  is never logged. Rotating it invalidates all outstanding tokens (that is fine: they are short).
* Missing on purpose: TLS, per-IP rate limiting, token refresh endpoint (clients ask again), metrics.

## Testing

```bash
uv run pytest -q
uv run ruff check
python mutate10.py            # RUNNER="uv run pytest -q -x" python mutate10.py to pick the test command
```

* **213 tests**: JWT (RFC 7515 vector, algorithm confusion, expiry/`nbf` boundaries, garbage input), grants
  (allow-list), settings, rate limiter boundaries, every rule of the service for every role, the HTTP layer, the CLI
  (including the deterministic demo), consistency of `compose.yaml` / `livekit.yaml` / `Dockerfile` / `.env.example`,
  and end-to-end tests over a real socket and a separate `serve` process.
* **Mutation check** (`mutate10.py`): 101 deliberate bugs applied one at a time to a copy of the code and the
  deployment files (boundaries, removed checks, widened grants, longer lifetimes, token leaks into the audit trail,
  a port or a user changed in the compose/Docker files...). **101/101 killed**. The first run stopped at mutant 74 (a typo in the script's pattern) after four survivors; each survivor pointed at a missing test (minimum secret length, inclusive bounds of numeric settings, rounding of `Retry-After`, `ttl_seconds=true`), and those tests were added.
* Flakiness: the whole suite ran 8 times in a row and the real-socket file 10 times, without a failure.

### What was *not* verified

* **No LiveKit server was available.** The claim names (`video.roomJoin`, `room`, `canPublish`, `canSubscribe`,
  `canPublishData`, `canPublishSources`, `hidden`, `metadata`, `attributes`) were checked against LiveKit's
  authentication documentation, and the signature against the RFC 7515 vector, but **no token from this service was
  presented to a real LiveKit server or SDK**. The `name` claim is what LiveKit's SDKs set; the docs page does not list it.
* **`docker compose up` and `docker build` were not run** (no Docker daemon). The image tag
  `livekit/livekit-server:v1.8` and the `LIVEKIT_KEYS` environment variable are written from memory of LiveKit's
  documentation: check both. The compose/Docker files are covered only by the consistency tests above.
* The test run used a small pytest stand-in because the build sandbox has no package index (`pytest`, `ruff`,
  `httpx`, `fastapi` could not be installed). The tests are plain pytest style, but **`uv run pytest` and
  `uv run ruff check` have not been run by the author**: run them once.
* Only Python 3.13 was available with the dependencies. `requires-python = ">=3.11"` is declared but untested here
  (CI runs 3.11 and 3.13).
* `uv sync` was not run, so there is no `uv.lock`; commit the one your first sync creates.
* Starlette is used instead of FastAPI (FastAPI was not installable here); the routes would port directly.
* No load test.

## Layout

```text
src/livekit_tokens/
  jwt.py        HS256 sign / verify (standard library)
  grants.py     role -> LiveKit video grant (the allow-list)
  token.py      builds the claims
  settings.py   environment configuration with strict checks
  ratelimit.py  sliding window
  db.py         schema, append-only audit triggers
  service.py    all rules: sessions, who joins as what, ttl, rate limit, audit
  models.py     request bodies (pydantic, extra fields forbidden)
  api.py        Starlette routes, error mapping
  cli.py        init-db, serve, inspect-token, demo
compose.yaml  Dockerfile  .env.example  deploy/livekit.yaml
tests/          213 tests (asgi.py = in-process ASGI client)
mutate10.py     mutation check
```

License: MIT (put your name in `LICENSE`).
