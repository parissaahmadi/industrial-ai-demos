import hashlib
import sqlite3
import threading

import pytest

from fwd_migrator.errors import IntegrityProblem, MigrationError, MigrationFailed
from fwd_migrator.runner import Migrator
from helpers import BASE, EXAMPLES, NOW, make, query, tables


def test_applies_in_order_and_records_the_history(tmp_path):
    m = make(tmp_path, BASE)
    done = m.migrate()
    assert [(a.version, a.name) for a in done] == [(1, "create_t"), (2, "add_u")]
    assert tables(m) == {"t", "u", "schema_migrations"}
    rows = query(m, "SELECT version, name, checksum, applied_at, applied_by FROM schema_migrations ORDER BY version")
    assert rows[0][:2] == (1, "create_t") and rows[0][3:] == ("2026-10-12T06:00:00Z", "ops.a")
    assert rows[1][2] == hashlib.sha256(BASE["0002_add_u.sql"].encode()).hexdigest()


def test_second_run_is_a_no_op(tmp_path):
    m = make(tmp_path, BASE)
    m.migrate()
    assert m.migrate() == []
    assert query(m, "SELECT count(*) FROM schema_migrations") == [(2,)]


def test_target_stops_early_and_a_later_run_continues(tmp_path):
    m = make(tmp_path, BASE)
    assert [a.version for a in m.migrate(target=1)] == [1]
    assert tables(m) == {"t", "schema_migrations"}
    assert [a.version for a in m.migrate()] == [2]
    with pytest.raises(MigrationError, match="target version 9 does not exist"):
        m.migrate(target=9)


def test_a_directory_with_no_files_is_a_clean_no_op(tmp_path):
    m = make(tmp_path, {})
    assert m.migrate() == [] and m.status() == []


def test_dry_run_changes_nothing_not_even_the_history_table(tmp_path):
    m = make(tmp_path, BASE)
    planned = m.migrate(dry_run=True)
    assert [a.version for a in planned] == [1, 2]
    assert tables(m) == set()  # no schema_migrations table either
    m.migrate()
    assert m.migrate(dry_run=True) == []


def test_dry_run_reports_the_same_failure_a_real_run_would(tmp_path):
    m = make(tmp_path, {**BASE, "0003_bad.sql": "INSERT INTO nowhere VALUES (1);"})
    with pytest.raises(MigrationFailed, match="0003_bad"):
        m.migrate(dry_run=True)
    assert tables(m) == set()


def test_failure_rolls_back_the_whole_migration_but_keeps_earlier_ones(tmp_path):
    files = {**BASE, "0003_half.sql": "CREATE TABLE half (i);\nINSERT INTO nowhere VALUES (1);\n"}
    m = make(tmp_path, files)
    with pytest.raises(MigrationFailed) as e:
        m.migrate()
    assert (e.value.version, e.value.name) == (3, "half") and "statement 2" in str(e.value)
    assert tables(m) == {"t", "u", "schema_migrations"}  # 'half' is gone, 1 and 2 stayed
    assert [r[0] for r in query(m, "SELECT version FROM schema_migrations")] == [1, 2]


def test_a_failed_statement_leaves_data_changes_rolled_back_too(tmp_path):
    files = {
        "0001_a.sql": "CREATE TABLE a (id INTEGER PRIMARY KEY, n INTEGER NOT NULL);\nINSERT INTO a (n) VALUES (1), (2);",
        "0002_b.sql": "UPDATE a SET n = n * 10;\nINSERT INTO a (n) VALUES (NULL);",
    }
    m = make(tmp_path, files)
    with pytest.raises(MigrationFailed):
        m.migrate()
    assert query(m, "SELECT n FROM a ORDER BY id") == [(1,), (2,)]


def test_a_fixed_file_can_be_retried_after_a_failure(tmp_path):
    m = make(tmp_path, {**BASE, "0003_x.sql": "INSERT INTO nowhere VALUES (1);"})
    with pytest.raises(MigrationFailed):
        m.migrate()
    (m.directory / "0003_x.sql").write_text("CREATE TABLE x (i);", encoding="utf-8")  # never applied: may change
    assert [a.version for a in m.migrate()] == [3]


def test_foreign_key_violation_left_behind_rolls_back(tmp_path):
    files = {
        "0001_a.sql": "CREATE TABLE p (id INTEGER PRIMARY KEY);\nCREATE TABLE c (id INTEGER PRIMARY KEY, p_id INTEGER);\n"
                      "INSERT INTO c (p_id) VALUES (42);",
        "0002_fk.sql": "CREATE TABLE c2 (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p (id));\n"
                       "INSERT INTO c2 (p_id) VALUES (7);",
    }
    m = make(tmp_path, {"0001_a.sql": files["0001_a.sql"]})
    m.migrate()
    # DEFERRED so the INSERT itself passes; only the post-migration foreign_key_check catches it
    (m.directory / "0002_fk.sql").write_text(
        "CREATE TABLE c2 (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p (id) DEFERRABLE INITIALLY DEFERRED);\n"
        "INSERT INTO c2 (p_id) VALUES (7);", encoding="utf-8")
    with pytest.raises(MigrationFailed, match="foreign-key violation"):
        m.migrate()
    assert "c2" not in tables(m)


# ---------------------------------------------------------------- history integrity
def test_editing_an_applied_file_is_refused_and_nothing_new_runs(tmp_path):
    m = make(tmp_path, BASE)
    m.migrate(target=1)
    (m.directory / "0001_create_t.sql").write_text(BASE["0001_create_t.sql"] + "\n-- harmless?", encoding="utf-8")
    write_new = m.directory / "0003_new.sql"
    write_new.write_text("CREATE TABLE fresh (i);", encoding="utf-8")
    with pytest.raises(IntegrityProblem, match="changed after it was applied"):
        m.migrate()
    assert tables(m) == {"t", "schema_migrations"}  # neither 0002 nor 0003 ran


def test_line_ending_changes_are_not_drift(tmp_path):
    m = make(tmp_path, {"0001_a.sql": "CREATE TABLE a (i);\nCREATE TABLE b (i);\n"})
    m.migrate()
    (m.directory / "0001_a.sql").write_bytes(b"CREATE TABLE a (i);\r\nCREATE TABLE b (i);\r\n")
    assert m.verify() == [] and m.migrate() == []


def test_adding_the_destructive_directive_later_counts_as_editing(tmp_path):
    m = make(tmp_path, {"0001_a.sql": "CREATE TABLE a (i);"})
    m.migrate()
    (m.directory / "0001_a.sql").write_text("-- migrate: allow-destructive\nCREATE TABLE a (i);", encoding="utf-8")
    assert [p.kind for p in m.verify()] == ["checksum_mismatch"]


def test_deleted_file_is_detected(tmp_path):
    m = make(tmp_path, {**BASE, "0003_c.sql": "CREATE TABLE c (i);"})
    m.migrate()
    (m.directory / "0003_c.sql").unlink()
    assert [(p.kind, p.version) for p in m.verify()] == [("missing_file", 3)]
    with pytest.raises(IntegrityProblem, match="no longer on disk"):
        m.migrate()


def test_renamed_file_is_detected(tmp_path):
    m = make(tmp_path, BASE)
    m.migrate()
    (m.directory / "0002_add_u.sql").rename(m.directory / "0002_add_users.sql")
    assert [(p.kind, p.version) for p in m.verify()] == [("renamed", 2)]


def test_a_never_applied_version_below_the_newest_applied_one_is_out_of_order(tmp_path):
    """Sequential runs can't produce this, but a restored or hand-edited database can: 1 and 3 recorded, 2 not."""
    files = {**BASE, "0003_c.sql": "CREATE TABLE c (i);"}
    m = make(tmp_path, files)
    m.migrate(target=1)
    from fwd_migrator.files import load_migrations

    third = load_migrations(m.directory)[2]
    conn = sqlite3.connect(m.db_path)
    conn.execute("INSERT INTO schema_migrations VALUES (3, ?, ?, 'x', 'hand.edit', 0)", (third.name, third.checksum))
    conn.commit()
    conn.close()
    assert [(p.kind, p.version) for p in m.verify()] == [("out_of_order", 2)]
    with pytest.raises(IntegrityProblem, match="never applied"):
        m.migrate()
    assert "u" not in tables(m)  # 0002 was NOT slipped in behind 0003


def test_all_problems_are_listed_not_just_the_first(tmp_path):
    m = make(tmp_path, {**BASE, "0003_c.sql": "CREATE TABLE c (i);"})
    m.migrate()
    (m.directory / "0001_create_t.sql").write_text("CREATE TABLE t (id INTEGER);", encoding="utf-8")
    (m.directory / "0003_c.sql").unlink()
    assert sorted(p.kind for p in m.verify()) == ["checksum_mismatch", "missing_file"]


def test_status_reports_every_state(tmp_path):
    m = make(tmp_path, {**BASE, "0003_c.sql": "CREATE TABLE c (i);"})
    m.migrate(target=2)
    assert [(r.version, r.state) for r in m.status()] == [(1, "applied"), (2, "applied"), (3, "pending")]
    (m.directory / "0002_add_u.sql").write_text("CREATE TABLE u (id INTEGER);", encoding="utf-8")
    assert [(r.version, r.state) for r in m.status()] == [(1, "applied"), (2, "drifted"), (3, "pending")]
    (m.directory / "0002_add_u.sql").unlink()
    (m.directory / "0003_c.sql").unlink()
    assert [(r.version, r.state) for r in m.status()] == [(1, "applied"), (2, "missing")]


def test_history_table_is_append_only_in_the_database_itself(tmp_path):
    m = make(tmp_path, BASE)
    m.migrate()
    conn = sqlite3.connect(m.db_path)
    for sql in ("UPDATE schema_migrations SET checksum = 'x'", "DELETE FROM schema_migrations"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    conn.close()


def test_read_only_calls_do_not_create_the_history_table(tmp_path):
    m = make(tmp_path, BASE)
    assert [r.state for r in m.status()] == ["pending", "pending"] and m.verify() == []
    assert tables(m) == set()


# ---------------------------------------------------------------- concurrency
def test_history_is_read_under_the_lock_not_before_it(tmp_path):
    """Deterministic race: runner A does all the work in the instant before runner B takes its lock."""
    a = make(tmp_path, BASE)
    b = Migrator(a.db_path, a.directory, clock=lambda: NOW, actor="ops.b")
    fired = []

    def a_runs_first():
        if not fired:
            fired.append(True)
            assert [x.version for x in a.migrate()] == [1, 2]

    b._before_lock = a_runs_first
    assert b.migrate() == []  # B must notice that A already applied everything; no duplicate-key crash
    assert fired and query(a, "SELECT count(*), min(applied_by) FROM schema_migrations") == [(2, "ops.a")]


def race(tmp_path, n_runners=6, n_migrations=8):
    files = {f"{i:04d}_step_{i}.sql": f"CREATE TABLE s{i} (i INTEGER);\nINSERT INTO s{i} VALUES ({i});" for i in range(1, n_migrations + 1)}
    first = make(tmp_path, files)
    runners = [Migrator(first.db_path, first.directory, clock=lambda: NOW, actor=f"ops.{n}") for n in range(n_runners)]
    results, errors = [], []
    barrier = threading.Barrier(len(runners))

    def go(runner):
        barrier.wait()
        try:
            results.append([a.version for a in runner.migrate()])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return first, results, errors


def test_threads_applying_at_once_apply_each_migration_exactly_once(tmp_path):
    for round_no in range(15):  # a race that fails "sometimes" must fail within these rounds
        first, results, errors = race(tmp_path / f"r{round_no}")
        assert errors == [], errors
        assert sorted(v for got in results for v in got) == list(range(1, 9))  # each version applied by one runner
        assert query(first, "SELECT count(*) FROM schema_migrations") == [(8,)]
        assert all(query(first, f"SELECT count(*) FROM s{i}") == [(1,)] for i in range(1, 9))


# ---------------------------------------------------------------- the shipped examples
def test_example_migrations_build_the_expected_schema(tmp_path):
    m = Migrator(tmp_path / "ex.sqlite3", EXAMPLES, clock=lambda: NOW, actor="ops.a")
    assert [a.version for a in m.migrate()] == [1, 2, 3, 4, 5]
    assert tables(m) == {"assets", "work_orders", "schema_migrations"}  # legacy_notes dropped by 0005
    assert query(m, "SELECT DISTINCT site FROM assets") == [("unassigned",)]
    assert m.verify() == []
