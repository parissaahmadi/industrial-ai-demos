# event-archive-api

A searchable archive for plant events and alarms, with **Persian-aware full-text search**, **voice-note
transcription behind a pluggable (mock) engine**, and a **tamper-evident audit trail**.

> **Demo data is fictional.** Assets (`PMP-0001`, `GBX-0004`, ...), brands (Vortex, Helix, Ironwood, Nimbus,
> Quanta), people (`op.b`, `adm.d`, ...) and readings are all invented. Nothing comes from a real site or company.

What it shows (each item is covered by tests, see [Testing](#testing)):

| Topic | What is implemented |
|---|---|
| Search | SQLite FTS5; user text is *never* passed to `MATCH` as written (tokenised, every word quoted); AND / `"phrases"` / `prefix*`; filters (asset, kind, severity, time window, has-recording); column scope (`message` / `transcript`); newest-first with an opaque **keyset cursor** (stable while new events arrive) or bm25 relevance with offset |
| Persian text | One normaliser for index *and* query: Arabic `ي ك ة` → Persian, digits of every script → ASCII, ZWNJ → space, diacritics/tatweel removed, NFKC |
| Ingest | Idempotent delivery by `(source, source_ref)`: same content → replay (`200`), different content → `409`; UTC-normalised timestamps, naive timestamps and far-future events rejected |
| Transcription | `Transcriber` protocol + deterministic `MockTranscriber`; job queue with attempts, exponential back-off with cap, leases (crashed worker is recovered), crash-loop stop (`dead`), permanent vs transient errors, manual retry; late results of a replaced worker are discarded |
| Human review | Low-confidence transcripts are flagged; a person can correct them (optimistic `version`); the machine text is kept; the index follows |
| Audit | Hash-chained, append-only (SQLite triggers), written in the *same transaction* as the change; denied attempts and every search are recorded; stores fingerprints (SHA-256), never message or transcript text |
| Redaction | Removes message, tags, audio, transcript and index entry; leaves a tombstone and an audit entry with the reason |
| Access | Bearer tokens (stored as SHA-256), four roles: `viewer`, `operator`, `admin`, `auditor` |

**Not speech recognition.** `MockTranscriber` reads a made-up container (`FAKEVOX`) whose payload is a JSON
script, so every path of the pipeline can be exercised offline and deterministically. A real engine only has to
implement `transcribe(audio: bytes) -> Transcript` (see `src/event_archive/transcribe.py`); none is included.

## Run it

```bash
uv sync
uv run event-archive demo                      # scripted, offline, fixed clock (output below)
uv run event-archive init-db --db archive.sqlite3   # prints one token per fictional user, once
uv run event-archive serve --db archive.sqlite3 --worker      # API on 127.0.0.1:8000, worker thread inside
# or the worker as its own process (same database file, safe to run next to the server):
uv run event-archive worker --db archive.sqlite3
uv run event-archive verify-audit --db archive.sqlite3         # exit code 1 if the chain is broken
```

## The scripted demo (real output)

`uv run event-archive demo` (`python -m event_archive demo`) loads 20 fictional events and 5 recordings and walks
through the features. Output of this repository's code, unedited (clock is fixed, so it is the same every run):

```text

== 1. loaded fictional plant data
{
  "events": 20,
  "by_kind": {
    "alarm": 9,
    "event": 5,
    "note": 6
  },
  "by_severity": {
    "critical": 3,
    "info": 11,
    "warning": 6
  },
  "recordings": {
    "pending": 5
  },
  "audit_entries": 25
}

== 2. transcription worker (mock engine; one recording fails twice before it works)
  t+  0s  {"claimed": 5, "done": 3, "retry": 1, "failed": 1, "dead": 0, "stale": 0}
  t+ 30s  {"claimed": 1, "done": 0, "retry": 1, "failed": 0, "dead": 0, "stale": 0}
  t+ 90s  {"claimed": 1, "done": 1, "retry": 0, "failed": 0, "dead": 0, "stale": 0}
  t+ 95s  {"claimed": 0, "done": 0, "retry": 0, "failed": 0, "dead": 0, "stale": 0}
  event #13: status=done attempts=3 error=None
  event #20: status=failed attempts=1 error=FAKEVOX payload is damaged

== 3. search: pump
  #12  06:50 PMP-0001 event info     [[pump]] restarted after bearing replacement
  #3   04:25 PMP-0001 alarm critical bearing temperature above 90 c, [[pump]] tripped
  #2   04:12 PMP-0001 alarm warning  bearing temperature above 75 c on [[pump]] drive end
  #1   04:00 PMP-0001 event info     vortex vx-200 [[pump]] started, discharge pressure 6.2 bar

== 4. search with Persian digits and spelling: «PMP-۰۰۰۱ seal» and «خنک کننده»
  #18  09:00 PMP-0001 alarm warning  [[pmp]]-[[0001]] flow [[seal]]
  #8   05:35 FAN-0003 note  info     موتور فن [[خنک]] [[کننده]] بازرسی شد؛ لرزش قابل توجه نبود

== 5. search inside transcripts only: «روغن»
  #10  06:06 GBX-0004 note  info     نشت [[روغن]] از آب بند سطح [[روغن]] پایین است

== 6. filters: critical alarms of the day, newest first
  #15  07:50 CMP-0002 alarm critical Compressor discharge temperature high-high
  #9   06:00 GBX-0004 alarm critical Ironwood IW-7 gearbox: oil leak detected at output seal
  #3   04:25 PMP-0001 alarm critical Bearing temperature above 90 C, pump tripped

== 7. hostile query is treated as plain words: «pump" OR NEAR(a b) message:x*»
  (no results)

== 8. low-confidence transcript flagged for review
{
  "status": "done",
  "confidence": 0.73,
  "needs_review": true,
  "text": "نشت روغن از آب‌بند سطح روغن پایین است"
}

== 9. after a human correction (machine text is kept)
{
  "needs_review": false,
  "corrected": true,
  "text": "نشت روغن از آب‌بند؛ سطح روغن خیلی پایین است",
  "original_text": "نشت روغن از آب‌بند سطح روغن پایین است"
}
  #10  06:06 GBX-0004 note  info     نشت روغن از آب بند؛ سطح روغن [[خیلی]] [[پایین]] است

== 10. after redacting event #9
  (no results)

== 11. audit trail (last 6 of 42 entries)
  #37  view.a         search               events     85328ad37989
  #38  view.a         search               events     fdad7553a126
  #39  op.b           transcript.correct   event:10   4a678f2e6512
  #40  view.a         search               events     af90efb9311e
  #41  adm.d          event.redact         event:9    cd6820946346
  #42  view.a         search               events     51d6c425ec13
  {"ok": true, "count": 42, "head": "51d6c425ec13fae78933f507656b6eee3446b54712aacada950a17ae2081d327", "broken_at": null, "reason": null}

== 12. after tampering with row 5 directly in the database
{
  "ok": false,
  "count": 4,
  "head": null,
  "broken_at": 5,
  "reason": "row content does not match its hash"
}
```

Reading it:

* Step 2: five recordings are claimed; three finish, one is a transient failure (`retry`, back-off 30 s then 60 s,
  finished on the third attempt), one has a damaged payload (`failed`, permanent, not retried).
* Step 4: `PMP-۰۰۰۱` (Persian digits) finds `PMP-0001`; `خنک کننده` (Persian *kaf*) finds a note typed with the Arabic *kaf*
  and a ZWNJ.
* Step 7: a hostile query is searched as plain words. It is not an error and it is not interpreted.
* Steps 8-9: `needs_review: true` because the average confidence is 0.73 (< 0.85); after a correction the flag is
  cleared, the machine text is kept as `original_text`, and the new wording is searchable.
* Step 12 modifies the database with the protection trigger removed, as someone with raw database access could;
  verification names row 5.

## Over HTTP (real session)

Real session against `serve --worker` on this repository's code (tokens shortened; the clock is the real one):

```bash
curl -H "Authorization: Bearer $OP" -H "Content-Type: application/json" localhost:8765/events -d '{
  "occurred_at": "2026-10-12T06:06:00Z", "asset_id": "GBX-0004", "kind": "note", "severity": "info",
  "message": "نشت روغن از آب‌بند خروجی گیربکس مشاهده شد", "tags": ["نشت", "روغن"],
  "source": "shift-log", "source_ref": "note-0042"}'
# 201 {"id": 1, "occurred_at": "2026-10-12T06:06:00Z", "asset_id": "GBX-0004", ..., "tags": ["روغن", "نشت"], "recording": null}

# the same request again: 200, headers  location: /events/1  and  idempotent-replayed: true

curl -X PUT -H "Authorization: Bearer $OP" -H "Content-Type: audio/x-fakevox" --data-binary @clip.bin localhost:8765/events/1/recording
# 201 ... "recording": {"status": "pending", "attempts": 0, ..., "size": 211}

# a second later the background worker has finished:
curl -H "Authorization: Bearer $VIEWER" localhost:8765/events/1/transcript
# {"status": "done", "language": "fa", "confidence": 0.74, "needs_review": true, "corrected": false, "version": 1,
#  "text": "نشت روغن از آب‌بند سطح روغن پایین است", "original_text": null, ...}

curl -G -H "Authorization: Bearer $VIEWER" --data-urlencode "q=سطح روغن" --data-urlencode "in=transcript" localhost:8765/events/search
# {"items": [{"id": 1, ..., "snippet": "نشت [[روغن]] از آب بند [[سطح]] [[روغن]] پایین است", "score": ...}], "count": 1, ...}

curl -H "Authorization: Bearer $VIEWER" localhost:8765/audit
# 403 {"type": "forbidden", "title": "forbidden", "status": 403, "detail": "the role 'viewer' may not do this (audit)"}

curl -H "Authorization: Bearer $AUDITOR" localhost:8765/audit/verify
# {"ok": true, "count": 10, "head": "5249273550a001e4...", "broken_at": null, "reason": null}
```

The audit entries of that session (`GET /audit`, as the auditor):

```text
1 system:worker token.issue view.a d24b20f7d016
2 system:worker token.issue op.b 62a1c8600607
3 system:worker token.issue op.c c356c998a880
4 system:worker token.issue adm.d bd63ac2965af
5 system:worker token.issue aud.e aef05cb453db
6 op.b event.ingest event:1 dac1214879c1
7 op.b recording.attach event:1 0c3265313df7
8 system:worker transcription.done event:1 c875d428c7b6
9 view.a search events 289b514a0fdc
10 view.a access.denied audit 5249273550a0
```

Note row 10: the viewer's refused attempt to read the audit trail *is* in the audit trail.

## API

| Method and path | Roles | Notes |
|---|---|---|
| `GET /health` | none | |
| `GET /me`, `GET /stats` | any token | |
| `POST /events` | operator, admin | `201`; replay `200` + `idempotent-replayed: true`; `409 source_ref_conflict` |
| `GET /events/{id}` | viewer, operator, admin | |
| `GET /events/search` | viewer, operator, admin | `q`, `in=all\|message\|transcript`, `asset_id`, `kind`, `severity`, `since` (inclusive), `until` (exclusive), `has_recording`, `sort=time\|relevance`, `limit` (1-100), `cursor` (time) / `offset` (relevance, max 1000) |
| `PUT /events/{id}/recording` | operator, admin | body = audio, `Content-Type: audio/x-fakevox`, max 1 MB; same audio again `200`; different audio `409` |
| `GET /events/{id}/transcript` | viewer, operator, admin | |
| `POST /events/{id}/transcript/correct` | operator, admin | `{"version", "text"}`; `409 stale_version` |
| `POST /events/{id}/transcript/retry` | operator, admin | only `failed` / `dead` |
| `POST /events/{id}/redact` | admin | `{"reason"}` |
| `POST /worker/run` | operator, admin | one pass of the worker |
| `GET /audit`, `GET /audit/verify` | auditor, admin | `verify?expected_head=<hash you saved>` |

Errors are RFC 9457-shaped: `{"type": "stale_version", "title": ..., "status": 409, "detail": ...}`. Unexpected
exceptions are logged on the server and answered with a generic `500`.

### Search syntax

`pump leak` (both words) · `"bearing temperature"` (adjacent) · `vibr*` (prefix, at least 2 letters) · nothing else.
There are no `OR`/`NOT`/column operators on purpose: the user's text is turned into quoted literals
(`src/event_archive/query.py`), so FTS5 syntax characters cannot cause syntax errors or change the query. `AND`
typed by a user is just the word "and".

## Design decisions and limits (read before reusing)

* **The audit chain detects, it does not prevent.** Triggers stop the application (and mistakes) from editing
  rows. Someone with raw write access can drop the triggers (the demo does) and, if they rewrite *every* row, recompute
  every hash. Removing rows from the *end* leaves a valid shorter chain. Both are caught only by keeping the head
  hash elsewhere (`verify-audit --expected-head`, or `GET /audit/verify?expected_head=`); the repository does not do
  that anchoring for you.
* **Audit content policy.** Searches are logged with the *normalised query text* and filters (useful for reviews,
  but a query can itself be sensitive: change it if yours is). Events and transcripts are logged as SHA-256 only,
  so redaction removes content for real.
* **Not logged:** failed authentication (`401`: there is no identity to attribute it to) and reads of `/audit`, `/stats`
  and `/events/{id}`.
* **Snippets** come from the normalised index text (so Arabic letters are already folded and digits are ASCII) and
  contain `[[` `]]` markers around matches. They are plain text; escape them before putting them in HTML.
* **Relevance** uses bm25 with column weights message 5 / transcript 3 / tags+asset 1. On a tiny corpus where the
  term is in most documents all scores are ~0 (a property of bm25; there is a test that uses a realistic corpus).
* **Time order wins by default** because alarm review is chronological; keyset cursors keep paging stable while new
  events arrive, offsets (relevance only) do not.
* **One writer at a time.** `BEGIN IMMEDIATE` plus a process lock keep the audit chain linear, including with
  the worker running as a second process on the same file (tested with two workers and with four writers).
  It is a deliberate SQLite-sized design; it is not a horizontally scaled one.
* **Audio is stored as a BLOB in SQLite** (limit 1 MB) to keep the demo self-contained. A real deployment would use
  object storage and keep the hash here.
* **Operational basics that are absent:** TLS, rate limiting, token expiry/rotation endpoints, retention jobs,
  backups, metrics. Tokens are issued by the CLI only.
* **Mock engine.** See above; the confidence thresholds (average < 0.85 or any segment < 0.6) are illustrative, not
  tuned on real recognition output.

## Testing

```bash
uv run pytest -q
uv run ruff check
python mutate9.py            # RUNNER="uv run pytest -q -x" python mutate9.py to pick the test command
```

* **248 tests**: text normalisation, query building (including a hostile-input corpus executed against a real FTS5
  table), audit chain tampering (edit each column, delete, insert, reorder, truncate), ingest/idempotency, search
  and paging, the worker (back-off boundaries, crashes, leases, races), corrections, redaction, permissions per role,
  the HTTP layer, and end-to-end tests over a real socket, two real worker connections on one file, concurrent
  writers, and `serve --worker` as a separate process.
* **Mutation check** (`mutate9.py`): 91 deliberate bugs applied one at a time to a copy of the code (boundaries,
  removed checks, widened permissions, weakened limits, text leaks into the audit trail...). **91/91 killed.** The first
  run killed 86; the five survivors each pointed at a missing test (tie-break order under a text query, offset
  boundary, retrying a `dead` job, redaction of a corrected transcript's original, early refusal by declared length),
  and those tests were added.
* Flakiness: one real-socket test (oversized upload) failed intermittently because the client kept sending after the server had already answered `413`; it now sends only the headers and reads the answer. After that fix the whole suite ran 8 times in a row (while a mutation run loaded the machine) and that test 25 times alone, without a failure.

### What was *not* verified

* The test run used a small pytest stand-in because the build sandbox has no package index (`pytest`, `ruff`,
  `httpx` could not be installed). The tests are plain pytest style (`parametrize`, `raises`, `tmp_path` and one
  generator fixture), but **`uv run pytest` and `uv run ruff check` have not been run by the author**: run them once.
* Only Python 3.13 was available with the dependencies. `requires-python = ">=3.11"` is declared but untested here
  (CI in `.github/workflows/ci.yml` runs 3.11 and 3.13).
* `uv sync` was not run, so there is no `uv.lock`; commit the one your first sync creates.
* The HTTP tests use a small in-process ASGI client plus real sockets with `urllib`; the `httpx` dev dependency is
  declared for your own use but nothing here imports it.
* No real speech engine, TLS, or load test.

## Layout

```text
src/event_archive/
  textnorm.py   normalisation shared by index and query
  query.py      user text -> safe FTS5 expression
  db.py         schema (FTS5 table, append-only audit triggers), seed users
  audit.py      hash chain: append, verify
  transcribe.py Transcriber protocol, MockTranscriber, FAKEVOX container
  service.py    all rules: ingest, search, recordings, worker, corrections, redaction, audit reads
  models.py     request bodies (pydantic, extra fields forbidden)
  api.py        Starlette routes, error mapping
  cli.py        init-db, serve, worker, verify-audit, demo
  sample_data.py fictional events and recordings
tests/          248 tests (helpers.py, asgi.py = in-process ASGI client)
mutate9.py      mutation check
```

License: MIT (put your name in `LICENSE`).
