import pytest

from fwd_migrator.errors import PolicyViolation
from helpers import make, query, tables

SETUP = {
    "0001_setup.sql": (
        "CREATE TABLE a (id INTEGER PRIMARY KEY, x TEXT);\n"
        "CREATE INDEX ia ON a (x);\n"
        "CREATE VIEW va AS SELECT * FROM a;\n"
        "CREATE TRIGGER ta AFTER INSERT ON a BEGIN SELECT 1; END;\n"
        "INSERT INTO a (x) VALUES ('keep');\n"
    )
}


def run_second(tmp_path, sql, directive=False):
    """Apply a known-good first migration, then try ``sql`` as migration 2."""
    files = dict(SETUP)
    files["0002_probe.sql"] = ("-- migrate: allow-destructive\n" if directive else "") + sql
    m = make(tmp_path, files)
    m.migrate(target=1)
    return m


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE b (id INTEGER PRIMARY KEY);",
        "CREATE INDEX ib ON a (id);",
        "CREATE VIEW vb AS SELECT 1;",
        "CREATE TRIGGER tb AFTER UPDATE ON a BEGIN SELECT 1; END;",
        "ALTER TABLE a ADD COLUMN y TEXT;",
        "ALTER TABLE a RENAME COLUMN x TO xx;",
        "ALTER TABLE a RENAME TO a2;",
        "ALTER TABLE a ADD COLUMN lvl TEXT DEFAULT 'low' CHECK (lvl IN ('low', 'high'));",  # SQLite runs quick_check
        "ALTER TABLE a ADD COLUMN req TEXT NOT NULL DEFAULT 'x';",
        "PRAGMA quick_check;",
        "PRAGMA foreign_key_check;",
        "INSERT INTO a (x) VALUES ('new');",
        "UPDATE a SET x = 'changed';",
        "CREATE TEMP TABLE scratch (i); INSERT INTO scratch VALUES (1); DROP TABLE scratch;",
        "ANALYZE;",
        "REINDEX;",
        "SELECT * FROM schema_migrations;",
    ],
)
def test_ordinary_changes_pass_without_any_directive(tmp_path, sql):
    m = run_second(tmp_path, sql)
    assert [a.version for a in m.migrate()] == [2]


@pytest.mark.parametrize(
    "sql,word",
    [
        ("DROP TABLE a;", "DROP TABLE"),
        ("DROP INDEX ia;", "DROP INDEX"),
        ("DROP VIEW va;", "DROP VIEW"),
        ("DROP TRIGGER ta;", "DROP TRIGGER"),
        ("DELETE FROM a;", "DELETE"),
        ("DELETE FROM a WHERE x = 'keep';", "DELETE"),
    ],
)
def test_destructive_statements_need_the_directive(tmp_path, sql, word):
    m = run_second(tmp_path, sql)
    with pytest.raises(PolicyViolation, match=f"{word} is destructive"):
        m.migrate()
    assert [r[0] for r in query(m, "SELECT version FROM schema_migrations")] == [1]
    assert query(m, "SELECT x FROM a") == [("keep",)]  # nothing was dropped or deleted


@pytest.mark.parametrize(
    "sql",
    ["DROP TABLE a;", "DROP INDEX ia;", "DROP VIEW va;", "DROP TRIGGER ta;", "DELETE FROM a;"],
)
def test_with_the_directive_they_run(tmp_path, sql):
    m = run_second(tmp_path, sql, directive=True)
    assert [a.version for a in m.migrate()] == [2]


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE ':memory:' AS other;",
        "PRAGMA foreign_keys = OFF;",
        "PRAGMA writable_schema = ON;",
        "PRAGMA table_info(a);",
        "COMMIT;",
        "ROLLBACK;",
        "BEGIN;",
        "SAVEPOINT s;",
        "CREATE VIRTUAL TABLE v USING fts5(body);",
        "SELECT load_extension('x');",
    ],
)
@pytest.mark.parametrize("directive", [False, True])
def test_never_allowed_even_with_the_directive(tmp_path, sql, directive):
    m = run_second(tmp_path, sql, directive=directive)
    with pytest.raises(PolicyViolation):
        m.migrate()
    assert [r[0] for r in query(m, "SELECT version FROM schema_migrations")] == [1]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO schema_migrations VALUES (99, 'forged', '" + "0" * 64 + "', 'x', 'x', 0);",
        "UPDATE schema_migrations SET checksum = '" + "0" * 64 + "';",
        "DELETE FROM schema_migrations;",
        "DROP TABLE schema_migrations;",
        "ALTER TABLE schema_migrations ADD COLUMN extra TEXT;",
        "ALTER TABLE schema_migrations RENAME TO hidden;",
        "CREATE TRIGGER sneaky AFTER INSERT ON schema_migrations BEGIN SELECT 1; END;",
        "CREATE INDEX sneaky ON schema_migrations (name);",
    ],
)
@pytest.mark.parametrize("directive", [False, True])
def test_a_migration_cannot_forge_or_alter_history(tmp_path, sql, directive):
    m = run_second(tmp_path, sql, directive=directive)
    with pytest.raises(PolicyViolation, match="schema_migrations"):
        m.migrate()
    rows = query(m, "SELECT version FROM schema_migrations")
    assert [r[0] for r in rows] == [1]


def test_a_trigger_cannot_be_used_to_write_history_later(tmp_path):
    files = dict(SETUP)
    files["0002_trigger.sql"] = (
        "CREATE TRIGGER forge AFTER INSERT ON a BEGIN "
        "INSERT INTO schema_migrations VALUES (99, 'x', '" + "0" * 64 + "', 'x', 'x', 0); END;"
    )
    files["0003_fire.sql"] = "INSERT INTO a (x) VALUES ('boom');"  # fires the trigger while policed
    m = make(tmp_path, files)
    with pytest.raises(PolicyViolation, match="schema_migrations"):
        m.migrate()
    assert [r[0] for r in query(m, "SELECT version FROM schema_migrations")] == [1, 2]
    assert query(m, "SELECT count(*) FROM a WHERE x = 'boom'") == [(0,)]


def test_a_denied_statement_rolls_back_the_statements_before_it(tmp_path):
    files = dict(SETUP)
    files["0002_mixed.sql"] = "CREATE TABLE fine (i);\nDROP TABLE a;\n"
    m = make(tmp_path, files)
    m.migrate(target=1)
    with pytest.raises(PolicyViolation):
        m.migrate()
    assert "fine" not in tables(m) and "a" in tables(m)


def test_the_runners_own_connection_is_not_left_restricted(tmp_path):
    """After a policed migration the bookkeeping INSERT (and the next migration) must still work."""
    m = make(tmp_path, {"0001_a.sql": "CREATE TABLE a (i);", "0002_b.sql": "CREATE TABLE b (i);"})
    assert [a.version for a in m.migrate()] == [1, 2]
    assert [r[0] for r in query(m, "SELECT version FROM schema_migrations")] == [1, 2]
