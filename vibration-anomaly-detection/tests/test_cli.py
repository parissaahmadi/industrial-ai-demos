import io
import sys

import pytest
from helpers import healthy_rows
from vibration_anomaly import cli
from vibration_anomaly import evaluate as ev
from vibration_anomaly.csvio import read_features, write_features
from vibration_anomaly.detectors import ALARM, OK, WARN, Status


class Capture:
    def __enter__(self):
        self.out, self.err, self._o, self._e = io.StringIO(), io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self._o, self._e


def test_parser_knows_every_command():
    p = cli.build_parser()
    for cmd in ("demo", "detect", "simulate"):
        assert p.parse_args([cmd] + (["x.csv"] if cmd == "detect" else ["--out", "x"] if cmd == "simulate" else [])).cmd == cmd
    a = p.parse_args(["detect", "f.csv"])
    assert (a.baseline_windows, a.warn_z, a.alarm_z) == (60, 4.0, 6.0)
    a = p.parse_args(["simulate", "--out", "o.csv"])
    assert (a.machine, a.fault, a.ramp, a.final, a.seed) == ("PMP-0001", "bearing", 60, 1.0, 0)
    with pytest.raises(SystemExit):
        p.parse_args(["simulate", "--fault", "rotor", "--out", "x"])


# ------------------------------------------------------------------ sparkline
def test_sparkline_scale_and_grouping():
    assert cli.sparkline([0.0] * 4) == " "
    assert cli.sparkline([12.0] * 4) == "█"
    assert cli.sparkline([0, 0, 0, 12, 0, 0, 0, 0]) == "█ "  # a group shows its largest value
    assert cli.sparkline([6.0] * 4) == "▄"  # halfway
    assert cli.sparkline([1.0] * 4) == "▁"  # 0.67 of a step rounds up, it is not cut off
    assert len(cli.sparkline([1.0] * 10)) == 3  # 4 + 4 + 2


def test_sparkline_clamps():
    assert cli.sparkline([-5.0] * 4) == " " and cli.sparkline([500.0] * 4) == "█"
    assert cli.sparkline([5.0, 5.0], lo=0, hi=10, group=1) == "▄▄"


def test_transitions():
    s = [Status(lv, 0, "rms", False) for lv in (OK, OK, WARN, WARN, ALARM, OK)]
    assert cli._transitions(s) == [(0, OK), (2, WARN), (4, ALARM), (5, OK)]
    assert cli._transitions([]) == []


# ------------------------------------------------------------------ simulate + detect through a file
def test_simulate_then_detect_a_fault(tmp_path):
    p = str(tmp_path / "bearing.csv")
    with Capture() as c:
        assert cli.main(["simulate", "--fault", "bearing", "--ramp", "30", "--out", p]) == 0
    assert "wrote 220 windows" in c.out.getvalue() and "onset at window 100" in c.out.getvalue()
    assert len(read_features(p)) == ev.TOTAL
    with Capture() as c:
        code = cli.main(["detect", p])
    out = c.out.getvalue()
    assert code == 2 and "ALARM" in out and "worst feature: band_hf" in out and "drift (CUSUM on rms) first flagged" in out


def test_detect_on_a_healthy_file_exits_zero(tmp_path):
    p = str(tmp_path / "ok.csv")
    write_features(healthy_rows(120), p)
    with Capture() as c:
        assert cli.main(["detect", p]) == 0
    assert "ALARM" not in c.out.getvalue() and "no drift flagged" in c.out.getvalue()
    assert c.out.getvalue().count("  window ") == 1  # only changes of level are listed, not every window
    assert "baseline: first 60 windows; 120 windows scored" in c.out.getvalue()


def test_detect_options_change_the_verdict(tmp_path):
    p = str(tmp_path / "f.csv")
    rows = healthy_rows(60) + [dict(r, rms=r["rms"] * 1.3) for r in healthy_rows(30, seed=2)]
    write_features(rows, p)
    with Capture():
        assert cli.main(["detect", p]) == 2
        assert cli.main(["detect", p, "--warn-z", "1000", "--alarm-z", "2000"]) == 0
        assert cli.main(["detect", p, "--baseline-windows", "61"]) == 2


@pytest.mark.parametrize("make", [lambda p: p.write_text("junk\n"), lambda p: write_features(healthy_rows(10), p)])
def test_detect_reports_bad_input_as_an_error(tmp_path, make):
    p = tmp_path / "bad.csv"
    make(p)  # too few windows for a baseline, or not a feature file
    with Capture() as c:
        assert cli.main(["detect", str(p)]) == 1
    assert c.err.getvalue().startswith("error:")


def test_detect_missing_file(tmp_path):
    with Capture() as c:
        assert cli.main(["detect", str(tmp_path / "nope.csv")]) == 1
    assert c.err.getvalue().startswith("error:")


def test_simulating_a_healthy_machine_does_not_claim_an_onset(tmp_path):
    with Capture() as c:
        assert cli.main(["simulate", "--fault", "none", "--out", str(tmp_path / "ok.csv")]) == 0
    assert "healthy throughout" in c.out.getvalue() and "onset" not in c.out.getvalue()
