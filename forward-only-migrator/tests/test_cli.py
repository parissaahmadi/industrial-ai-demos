import io

from fwd_migrator.cli import main, run_demo
from helpers import BASE, write


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out, err)
    return code, out.getvalue(), err.getvalue()


def setup(tmp_path):
    d = write(tmp_path / "migrations", BASE)
    return ["--db", str(tmp_path / "x.sqlite3"), "--dir", str(d), "--actor", "ops.a"], d


def test_status_up_verify_flow(tmp_path):
    common, _ = setup(tmp_path)
    code, out, _ = run("status", *common)
    assert code == 0 and "0001  pending" in out
    code, out, _ = run("up", *common, "--dry-run")
    assert code == 0 and "would apply 0002_add_u" in out and "rolled back" in out
    assert "pending" in run("status", *common)[1]
    code, out, _ = run("up", *common)
    assert code == 0 and "applied 0001_create_t" in out and "applied 0002_add_u" in out
    assert run("up", *common)[1].strip() == "nothing to do"
    assert run("verify", *common)[1].strip() == "history OK"


def test_verify_exits_2_on_drift_and_up_exits_1(tmp_path):
    common, d = setup(tmp_path)
    run("up", *common)
    (d / "0001_create_t.sql").write_text("CREATE TABLE t (id INTEGER);", encoding="utf-8")
    code, out, _ = run("verify", *common)
    assert code == 2 and "PROBLEM checksum_mismatch" in out
    code, out, err = run("up", *common)
    assert code == 1 and "integrity_problem" in err and out == ""
    assert "drifted" in run("status", *common)[1]


def test_errors_go_to_stderr_with_a_stable_code(tmp_path):
    common, d = setup(tmp_path)
    (d / "0001_create_t.down.sql").write_text("DROP TABLE t;", encoding="utf-8")
    code, out, err = run("status", *common)
    assert code == 1 and out == "" and "invalid_migration_file" in err and "forward-only" in err
    (d / "0001_create_t.down.sql").unlink()
    (d / "0003_bad.sql").write_text("DROP TABLE t;", encoding="utf-8")
    code, _, err = run("up", *common)
    assert code == 1 and "policy_violation" in err


def test_new_creates_the_next_numbered_file(tmp_path):
    common, d = setup(tmp_path)
    code, out, _ = run("new", "Add meter readings!", *common)
    assert code == 0 and (d / "0003_add_meter_readings.sql").exists()
    assert "Forward-only" in (d / "0003_add_meter_readings.sql").read_text(encoding="utf-8")
    code, _, err = run("new", "!!!", *common)
    assert code == 1 and "letters or digits" in err


def test_up_target(tmp_path):
    common, _ = setup(tmp_path)
    assert "0002" not in run("up", *common, "--target", "1")[1]
    assert run("up", *common, "--target", "9")[0] == 1


def test_scripted_demo_runs_to_the_end():
    out = io.StringIO()
    assert run_demo(out) == 0
    text = out.getvalue()
    assert "objects in the database afterwards: 0" in text
    assert "meter_readings table exists after the failure: False" in text
    assert "DROP TABLE is destructive" in text and "may not modify schema_migrations" in text
