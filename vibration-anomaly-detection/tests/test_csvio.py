import pytest

from helpers import healthy_rows
from vibration_anomaly.csvio import HEADER, read_features, write_features
from vibration_anomaly.features import FEATURES


def test_roundtrip_is_exact(tmp_path):
    rows = healthy_rows(5)
    p = tmp_path / "f.csv"
    write_features(rows, p)
    assert read_features(p) == rows
    assert p.read_text().splitlines()[0] == ",".join(HEADER) == "window," + ",".join(FEATURES)


def write(tmp_path, text):
    p = tmp_path / "f.csv"
    p.write_text(text, encoding="utf-8")
    return p


GOOD = "window," + ",".join(FEATURES) + "\n"


@pytest.mark.parametrize("text,needle", [
    ("", "line 1"),
    ("a,b,c\n", "line 1"),
    ("window,rms\n0,1\n", "line 1"),
    (GOOD, "no data rows"),
    (GOOD + "0,1,2,3\n", "line 2: expected 7 columns"),
    (GOOD + "0,1,2,3,4,5,6,7\n", "line 2: expected 7 columns"),
    (GOOD + "0,1,2,3,4,5,6\n2,1,2,3,4,5,6\n", "line 3: window numbers"),
    (GOOD + "1,1,2,3,4,5,6\n", "line 2: window numbers"),
    (GOOD + "x,1,2,3,4,5,6\n", "line 2: window numbers"),
    (GOOD + "-1,1,2,3,4,5,6\n", "line 2: window numbers"),
    (GOOD + "0,1,2,3,4,5,abc\n", "line 2: a value is not a number"),
    (GOOD + "0,1,2,3,4,5,nan\n", "line 2: values must be finite"),
    (GOOD + "0,1,2,3,4,5,inf\n", "line 2: values must be finite"),
    (GOOD + "0,1,2,3,4,5,-0.5\n", "line 2: values must be finite and not negative"),
])  # fmt: skip
def test_bad_files_are_errors_with_a_line_number(tmp_path, text, needle):
    with pytest.raises(ValueError) as e:
        read_features(write(tmp_path, text))
    assert needle in str(e.value)


def test_zero_is_a_valid_value_and_windows_may_be_many(tmp_path):
    rows = read_features(write(tmp_path, GOOD + "0,0,0,0,0,0,0\n1,1,1,1,1,1,1\n"))
    assert len(rows) == 2 and rows[0]["rms"] == 0.0


def test_missing_file(tmp_path):
    with pytest.raises(OSError):
        read_features(tmp_path / "nope.csv")
