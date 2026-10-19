import hashlib

import pytest
from fwd_migrator.errors import InvalidMigrationFile
from fwd_migrator.files import checksum_of, load_migrations
from fwd_migrator.sqlsplit import split_statements
from helpers import BASE, EXAMPLES, write


def load(tmp_path, files):
    return load_migrations(write(tmp_path / "m", files))


def test_loads_in_version_order_with_real_sha256(tmp_path):
    ms = load(tmp_path, {"0002_b.sql": "SELECT 2;", "0001_a.sql": "SELECT 1;", "README.md": "ignored"})
    assert [(m.version, m.name, m.filename) for m in ms] == [(1, "a", "0001_a.sql"), (2, "b", "0002_b.sql")]
    assert ms[0].checksum == hashlib.sha256(b"SELECT 1;").hexdigest()


@pytest.mark.parametrize(
    "name",
    ["1_a.sql", "001_a.sql", "0001-a.sql", "0001_A.sql", "0001_a b.sql", "0001_.sql", "0001__a.sql", "a_0001.sql", "0001.sql"],
)
def test_bad_names_are_rejected_not_skipped(tmp_path, name):
    with pytest.raises(InvalidMigrationFile, match="file name must look like"):
        load(tmp_path, {name: "SELECT 1;"})


@pytest.mark.parametrize("name", ["0001_a.down.sql", "0001_a.undo.sql", "down_0001.sql", "0001.rollback.sql"])
def test_down_migrations_are_explained_as_unsupported(tmp_path, name):
    with pytest.raises(InvalidMigrationFile, match="forward-only"):
        load(tmp_path, {name: "SELECT 1;"})


def test_a_legitimate_name_containing_down_is_fine(tmp_path):
    assert load(tmp_path, {"0001_add_down_payment.sql": "SELECT 1;"})[0].name == "add_down_payment"


def test_gap_duplicate_and_wrong_start(tmp_path):
    with pytest.raises(InvalidMigrationFile, match="0002 is missing"):
        load(tmp_path / "gap", {"0001_a.sql": "SELECT 1;", "0003_c.sql": "SELECT 3;"})
    with pytest.raises(InvalidMigrationFile, match="used by both"):
        load(tmp_path / "dup", {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"})
    with pytest.raises(InvalidMigrationFile, match="0001 is missing"):
        load(tmp_path / "start", {"0002_a.sql": "SELECT 1;"})
    with pytest.raises(InvalidMigrationFile, match="start at 0001"):
        load(tmp_path / "zero", {"0000_a.sql": "SELECT 1;"})


def test_missing_directory_empty_file_and_non_utf8(tmp_path):
    with pytest.raises(InvalidMigrationFile, match="not found"):
        load_migrations(tmp_path / "nope")
    with pytest.raises(InvalidMigrationFile, match="empty"):
        load(tmp_path / "e", {"0001_a.sql": "  \n"})
    d = tmp_path / "bin"
    d.mkdir()
    (d / "0001_a.sql").write_bytes(b"SELECT '\xff';")
    with pytest.raises(InvalidMigrationFile, match="UTF-8"):
        load_migrations(d)


def test_directives_are_strict(tmp_path):
    ms = load(tmp_path / "ok", {"0001_a.sql": "-- migrate: allow-destructive\n-- why\nSELECT 1;"})
    assert ms[0].allow_destructive is True
    with pytest.raises(InvalidMigrationFile, match="unknown directive 'allow-drop'"):
        load(tmp_path / "bad", {"0001_a.sql": "-- migrate: allow-drop\nSELECT 1;"})
    late = load(tmp_path / "late", {"0001_a.sql": "SELECT 1;\n-- migrate: allow-destructive\n"})
    assert late[0].allow_destructive is False  # only the leading comment block counts


def test_checksum_ignores_line_endings_but_not_content():
    assert checksum_of(b"A\r\nB\r\n") == checksum_of(b"A\nB\n")
    assert checksum_of(b"A\nB\n") != checksum_of(b"A\nB \n")


def test_checksum_covers_the_directive():
    assert checksum_of(b"-- migrate: allow-destructive\nDROP TABLE x;") != checksum_of(b"DROP TABLE x;")


def test_split_statements_handles_triggers_strings_and_comments():
    sql = (
        "-- header; with a semicolon\n"
        "CREATE TABLE a (x TEXT DEFAULT ';'); CREATE TABLE b (y);\n"
        "/* block; comment */\n"
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN UPDATE a SET x = 'q;'; INSERT INTO b VALUES (1); END;\n"
        ";;\n"
        "SELECT 1\n"
        "-- trailing comment\n"
    )
    out = split_statements(sql)
    assert len(out) == 4
    assert out[0].endswith("DEFAULT ';');") and out[1] == "CREATE TABLE b (y);"
    assert out[2].startswith("/* block; comment */\nCREATE TRIGGER") and out[2].endswith("END;")
    assert out[3].startswith("SELECT 1")


def test_a_comment_only_file_has_no_statements():
    assert split_statements("-- nothing here\n/* at all */\n") == []


def test_example_and_base_migrations_are_valid(tmp_path):
    assert [m.version for m in load_migrations(EXAMPLES)] == [1, 2, 3, 4, 5]
    assert len(load(tmp_path, BASE)) == 2
