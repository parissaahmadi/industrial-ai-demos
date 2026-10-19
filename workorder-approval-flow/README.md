# workorder-approval-flow

A small, fictional three-role approval workflow for maintenance work requests: **submitter → reviewer → supervisor**.
It is a JSON HTTP API on SQLite, built to show the parts of a workflow service that usually go wrong:
state transitions, a four-eyes rule, lost updates, retries, and an audit trail nobody can edit.

All people, assets and amounts are invented (`sub.a`, `rev.c`, `sup.e`, `AST-0042`). No real company data.

## Rules it enforces

| Rule | Behaviour |
|---|---|
| Drafts are private | Only the owner sees a draft. Anyone else gets `404`, the same answer as for a missing id. |
| Cost limit | Reviewers can finally approve up to **50,000,000 rial** (configurable). Above that, a reviewer's approval is only a first stage (`reviewed`). |
| Four eyes | A supervisor, who is a different person from the owner and from the first-stage reviewer, gives the final approval. Nobody decides their own request. |
| Return | Reviewer or supervisor sends a request back with a mandatory comment; the owner edits and resubmits (`revision + 1`, first-stage approval is cleared). |
| Concurrency | Every write carries the `version` the client read. A stale version gets `409 stale_version` with `current_version`. |
| Retries | `POST /requests` accepts an `Idempotency-Key`. Same key and body replays the answer (`200`, `Idempotent-Replayed: true`); same key with another body is `422`. |
| Audit | Every change writes an audit event in the **same transaction**. SQLite triggers reject `UPDATE`/`DELETE` on the audit table. |

```
draft --submit--> submitted --approve (cost <= limit)--> approved
  |                  |   \--approve (cost > limit)--> reviewed --approve--> approved
  |                  |-- reject --> rejected              |-- reject --> rejected
  |                  \-- return --> returned <-----------/ return
  |                                   |  (owner edits, then submit again: revision + 1)
  \-- withdraw (owner, any non-final state) --> withdrawn
```

## Run it

```bash
uv sync
uv run workorder-flow demo                 # scripted scenario over real HTTP (uvicorn on a free port)
uv run workorder-flow init-db --db w.db    # prints one bearer token per sample user, once
uv run workorder-flow serve --db w.db      # http://127.0.0.1:8000
curl -H "Authorization: Bearer <token>" localhost:8000/me
```

Only SHA-256 hashes of tokens are stored in the database. `issue-token <username>` adds another token.

### Endpoints

`GET /health` · `GET /me` · `GET /queue` · `POST|GET /requests` · `GET|PATCH /requests/{id}` ·
`GET /requests/{id}/events` · `POST /requests/{id}/submit` · `POST .../withdraw` · `POST .../decision`
(`{"version": n, "action": "approve|reject|return", "comment": "..."}`).

Errors are JSON in one shape: `{"type", "title", "status", "detail"}` plus extras (`errors` for validation, `current_version` for 409).

## A real run

Output of `workorder-flow demo` (a request costing 80,000,000 rial, i.e. above the reviewer limit):

```
sub.a  POST  /requests              -> 201 status=draft version=1  (cost > 50M limit)
sub.a  POST  /requests/1/submit     -> 200 status=submitted version=2
sub.b  GET   /requests/1            -> 404 request 1 does not exist  (other submitter)
sub.a  POST  /requests/1/decision   -> 403 a request in status 'submitted' can only be decided by a reviewer or supervisor; you are a submitter  (own request)
rev.c  POST  /requests/1/decision   -> 409 the request changed since you read it (you have version 1, it is now version 2); fetch it again and retry  (stale version)
rev.c  POST  /requests/1/decision   -> 200 status=reviewed version=3  (first stage)
rev.c  POST  /requests/1/decision   -> 403 a request in status 'reviewed' can only be decided by a supervisor; you are a reviewer  (same person again)
rev.d  POST  /requests/1/decision   -> 403 a request in status 'reviewed' can only be decided by a supervisor; you are a reviewer  (reviewer cannot finish)
sup.e  POST  /requests/1/decision   -> 200 status=approved version=4  (final)
sub.a  GET   /requests/1/events     -> 200
audit trail:
  #1 sub.a  created    -          -> draft
  #2 sub.a  submitted  draft      -> submitted
  #3 rev.c  reviewed   submitted  -> reviewed
  #4 sup.e  approved   reviewed   -> approved
```

Also checked by hand against `serve`: no token → `401` with `WWW-Authenticate: Bearer`; a typo'd field
(`estimated_cots`) → `422` naming the field instead of being silently ignored.

## Design decisions

- **Domain first, framework second.** `states.py` is pure (no SQLite, no HTTP), so the whole approval matrix is tested exhaustively. `service.py` owns transactions; `api.py` is a thin Starlette layer.
- **Check order in a decision:** visibility → role / four-eyes → version → transition validity. A stale client learns "stale" rather than a confusing "invalid transition", and an outsider learns nothing.
- **`BEGIN IMMEDIATE` + `UPDATE ... WHERE version = ?`** — the write and its audit row commit or roll back together.
- **Strict input:** pydantic `strict=True`, `extra="forbid"`: `"10"` is not a number, unknown fields are errors. 64 KB body limit, enforced with and without `Content-Length`.
- **No leaks:** unexpected errors become a generic `500`; the traceback goes to the server log only. Responses carry `Cache-Control: no-store`.
- **Injected clock** keeps tests deterministic.

## Tests

```bash
uv run pytest -q
uv run ruff check
python mutate6.py          # RUNNER="uv run pytest -q -x" python mutate6.py
```

109 tests: exhaustive state matrix, service rules, HTTP behaviour through an in-process ASGI client, and
end-to-end tests over a real uvicorn socket. `mutate6.py` applies 15 deliberate bugs one at a time to a copy
(limit boundary, owner self-approval, removed version check, lost update, visible drafts, no rollback, clear-text tokens,
rewritable audit rows, body limit, cache header, ...). **15 of 15 were caught.** The first run caught 13: one survivor was a
real gap (chunked bodies without `Content-Length` were untested, now covered), the other a mutation that changed nothing.

> **How these numbers were obtained.** The authoring sandbox had no PyPI access, so `pytest`, `ruff`, `fastapi` and
> `httpx` could not be installed. The 109 tests and the mutation run used a small pytest-compatible shim written for
> that purpose, with Starlette's and uvicorn's real code. `uv run pytest` and `uv run ruff check` had **not** been run
> by the author at the time of writing; run them once and treat any difference as a bug in this repo.

## Limitations

- **Starlette, not FastAPI** (same ASGI core; FastAPI could not be installed in the sandbox). There is no auto-generated OpenAPI page.
- **Authentication is bearer tokens only**: no login, expiry, rotation or rate limiting. Sample users are seeded. This is not an identity system.
- **One process, one SQLite file.** Requests are serialized by a lock and `BEGIN IMMEDIATE`; no horizontal scaling.
- Idempotency keys are never expired; there is no cleanup job.
- The 50,000,000 limit is a single global number, not per asset class or currency-aware.
- No notifications; "who is waiting" is only the `/queue` endpoint.

## Development

Python 3.11+, `uv`. `uv.lock` is created by the first `uv sync`; commit it.

## License

MIT, see `LICENSE`.
