# industrial-ai-demos

A collection of five small, self-contained Python demos for industrial software. They cover approval workflows, database migrations, Persian-aware search, remote-assist tokens, and vibration anomaly detection.

Each sub-directory is an **independent project** with its own `pyproject.toml`, `uv.lock`, tests, and README. They share nothing at runtime; the only reason they live together is to keep the portfolio navigable.

## The demos

| Directory | What it shows | Stack |
|---|---|---|
| [`workorder-approval-flow/`](workorder-approval-flow) | Three-role approval workflow (submitter → reviewer → supervisor) over a JSON HTTP API on SQLite. Four-eyes rule, optimistic concurrency, idempotency keys, audit trail. | Starlette, SQLite |
| [`forward-only-migrator/`](forward-only-migrator) | Forward-only SQL migration runner with SHA-256 checksums, one transaction per migration, a least-privilege SQL authorizer, and safe concurrency. | stdlib, SQLite |
| [`event-archive-api/`](event-archive-api) | Searchable archive for plant events and alarms. Persian-aware FTS5, pluggable transcription, hash-chained audit log, redaction. | Starlette, SQLite FTS5 |
| [`livekit-token-service/`](livekit-token-service) | Issues short-lived LiveKit access tokens for remote help, with role-scoped grants, rate limiting and an audit trail. | Starlette, JWT (stdlib) |
| [`vibration-anomaly-detection/`](vibration-anomaly-detection) | Explainable anomaly detection on synthetic vibration data: robust baseline, persistence rules, CUSUM drift. | stdlib (FFT) |

All data is fictional.

## How to run

Each demo is independent. To run any of them:

```bash
cd <demo-directory>
uv sync
uv run pytest -q        # run tests
# then follow the README inside that directory for the CLI command