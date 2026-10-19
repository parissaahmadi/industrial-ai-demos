# forward-only-migrator

A small SQL migration runner for SQLite where **history cannot be quietly rewritten**.
No down-migrations, no ORM, no dependencies (standard library only).

It exists to show the unglamorous parts of a migration tool done properly: checksums, ordering,
one transaction per migration, a least-privilege execution policy, and safety when two deploys run at once.

The sample schema is fictional plant equipment (`assets`, `work_orders`). No real company data.

## What it guarantees

| Guarantee | How |
|---|---|
| **Forward only** | There is no "down". A mistake is fixed by a new numbered file. `0001_x.down.sql` is rejected with an explanation. |
| **Applied files are frozen** | SHA-256 of each file is stored when it is applied. Edit it, delete it, or rename it and the next run refuses, naming the file. |
| **No look-alike checkout drift** | The checksum normalises `CRLF` to `LF`, so a Windows checkout is not "tampering". |
| **Gaps and strays are errors** | Versions must be `0001…N` with no gaps; a malformed `.sql` name is an error, never silently skipped. |
| **One transaction per migration** | The migration's SQL and its `schema_migrations` row commit together or not at all. SQLite DDL is transactional, so a failure leaves no half-built table. Earlier migrations stay applied. |
| **Safe under concurrency** | Each step takes `BEGIN IMMEDIATE` and re-reads history *inside* the lock; two runners never apply the same migration. |
| **Dry run is real** | `up --dry-run` executes the SQL in a transaction and rolls everything back, so SQL errors surface without changing the database. |
| **History table is append-only** | SQLite triggers reject `UPDATE`/`DELETE` on `schema_migrations`. |
| **Least privilege** | Migration SQL runs under an allow-list (below). |

### The execution "role"

SQLite has no users or `GRANT`s, so the role is an allow-list enforced with `set_authorizer` while migration SQL runs.
Anything not listed is denied. The runner's own bookkeeping runs unrestricted.

| | Statements |
|---|---|
| Allowed | `CREATE` TABLE / INDEX / VIEW / TRIGGER (and temp objects), `ALTER TABLE`, `INSERT`, `UPDATE`, `SELECT`, `REINDEX`, `ANALYZE` |
| Needs `-- migrate: allow-destructive` as a leading comment | `DROP` TABLE / INDEX / VIEW / TRIGGER, `DELETE` |
| Never allowed | `ATTACH`/`DETACH`, any `PRAGMA` except the read-only integrity checks (SQLite itself runs `quick_check` for `ADD COLUMN ... CHECK`), `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`, virtual tables, `load_extension`, **any change to `schema_migrations`** (a migration cannot forge its own history, not even through a trigger) |

The directive is part of the file, so it is part of the checksum and shows up in code review. Adding it to an already-applied file counts as editing it.
`docs/postgres-role.sql` sketches the PostgreSQL equivalent with real roles. It is a **design sketch, not run or tested**.

## Run it

```bash
uv sync
uv run fwd-migrate demo                                    # scripted tour on a throw-away directory
uv run fwd-migrate up      --db plant.sqlite3 --dir examples/migrations --actor ops.a
uv run fwd-migrate status  --db plant.sqlite3 --dir examples/migrations
uv run fwd-migrate verify  --db plant.sqlite3 --dir examples/migrations   # exit 2 if history and files disagree (CI gate)
uv run fwd-migrate up --dry-run ...                        # run, then roll everything back
uv run fwd-migrate new "add meter readings" --dir examples/migrations
```

Exit codes: `0` ok · `1` error (printed to stderr as `error [code]: ...`) · `2` `verify` found a problem.

## A real run

Output of `fwd-migrate demo`, trimmed only of blank lines (SQLite 3.45.1, Python 3.13):

```
$ fwd-migrate up --dry-run   (SQL runs, then everything is rolled back)
  would apply 0001_create_assets
  would apply 0002_create_work_orders
  would apply 0003_add_asset_site_and_index
  would apply 0004_backfill_default_site
  would apply 0005_drop_legacy_notes
  objects in the database afterwards: 0

$ fwd-migrate up
  applied 0001_create_assets
  ... (0002 to 0005)
$ fwd-migrate up   (again)
  applied 0 migration(s)

$ edit an applied migration, then: fwd-migrate verify
  PROBLEM checksum_mismatch: 0003: 0003_add_asset_site_and_index.sql changed after it was applied (recorded 82ec65017b1d…, now b46989d14a13…); write a new migration instead of editing an applied one
ERROR [integrity_problem] 0003: 0003_add_asset_site_and_index.sql changed after it was applied (...)
  verify after restore: history OK

$ add 0006 that breaks half-way
ERROR [migration_failed] migration 0006_add_meter_readings failed and was rolled back: statement 2: no such table: no_such_table
  meter_readings table exists after the failure: False
  0001 applied ... 0005 applied
  0006 pending      add_meter_readings

$ fix 0006 (it was never applied, so editing it is allowed) and run again
  applied 0006_add_meter_readings

$ 0007 tries DROP TABLE without the directive, 0007 is then rewritten to forge history
ERROR [policy_violation] 0007_cleanup.sql, statement 1: DROP TABLE is destructive; add '-- migrate: allow-destructive' at the top of the file if that is intended
ERROR [policy_violation] 0007_cleanup.sql, statement 1: migrations may not modify schema_migrations
  0007 pending      cleanup
```

## Design decisions

- **Why contiguous numbers.** Branches that both add `0004` should collide loudly in review, not interleave at deploy time. A missing number usually means a lost file.
- **Why re-read history under the lock.** Reading first and locking later is the classic double-apply race. A test makes it deterministic by letting runner A finish in the instant before runner B locks.
- **Why `executescript` is not used.** It issues an implicit `COMMIT`, which would break the one-transaction guarantee. Statements are split with `sqlite3.complete_statement` (so `;` in strings, comments and trigger bodies is safe) and run one by one.
- **Why a post-migration `PRAGMA foreign_key_check`.** `ALTER`/`CREATE` can leave deferred or pre-existing foreign-key violations that no single statement reports.
- **Out-of-order detection** protects against a restored or hand-edited database (e.g. `1` and `3` recorded, `2` not). Normal sequential runs cannot produce it.

## Tests

```bash
uv run pytest -q
uv run ruff check
python mutate7.py        # or: RUNNER="uv run pytest -q -x" python mutate7.py
```

119 tests: file rules and the SQL splitter, the execution policy (about 60 parametrised cases), the runner (rollback, drift, deletion, rename, out-of-order, dry run, foreign keys, history triggers), concurrency, the CLI, and the scripted demo.

`mutate7.py` applies 27 deliberate bugs to a copy, one at a time (drift checks removed, commit on failure, lazy locking, history read before the lock, the policy opened up or over-tightened in seven ways, gaps allowed, line-ending-sensitive checksums, a naive statement splitter, ...). **27 of 27 are caught.**
Two things worth knowing. A review of the policy found a real bug the first test set missed: `ALTER TABLE ... ADD COLUMN ... CHECK (...)` was denied, because SQLite runs `PRAGMA quick_check` internally; it is fixed and has its own test and mutation. And the number needed work: the first version of the concurrency test let the "lazy `BEGIN`" bug survive in 7 of 15 runs and the "read before the lock" bug in 1 of 15. It now runs 15 racing rounds and has a deterministic race test; over 30 repeats the unmutated code passed 30/30 and both bugs were caught 30/30.

> **How these numbers were obtained.** The authoring sandbox cannot reach PyPI, so `pytest` and `ruff` were not installed. The 119 tests, the mutation run and the repeats used a small pytest-compatible shim; the suite also passed on Python 3.11, 3.12 and 3.13 that way.
> `uv run pytest` and `uv run ruff check` were **not** run by the author. Treat any difference as a bug in this repo.

## Limitations

- **SQLite only.** The Postgres file in `docs/` is a sketch, not code that was run.
- **The policy sees statement kinds, not `WHERE` clauses.** `UPDATE` without a filter is allowed, and `ALTER TABLE ... DROP COLUMN` counts as `ALTER`, not destructive. Review still matters; the policy narrows what a mistake can do, it does not read intent.
- **Checksums are tamper-evident, not tamper-proof.** Someone with full write access can drop the history triggers and rewrite rows. The goal is to catch accidents and unreviewed edits, not an attacker with admin rights.
- **No down-migrations, no baselining** of an existing database, no per-environment migration sets.
- **Contiguity is strict.** Teams that merge long-lived branches will have to renumber unapplied files by hand.
- A runner waits up to 30 s for the write lock (`busy_timeout`) and then fails with SQLite's "database is locked".
- `--actor` is whatever string the caller passes; it is an audit label, not authentication.

## Development

Python 3.11+, `uv`. `uv.lock` is created by the first `uv sync`; commit it.

## License

MIT, see `LICENSE`.
