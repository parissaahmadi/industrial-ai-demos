"""Feature windows as CSV, read strictly: a bad file is an error with a line number, never silently skipped."""

from __future__ import annotations

import csv
import math
from pathlib import Path

from .features import FEATURES

HEADER = ["window", *FEATURES]


def write_features(rows: list[dict[str, float]], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i, r in enumerate(rows):
            w.writerow([i, *(repr(float(r[f])) for f in FEATURES)])


def read_features(path: str | Path) -> list[dict[str, float]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != HEADER:
            raise ValueError(f"line 1: expected the header {','.join(HEADER)}")
        rows = []
        for line, rec in enumerate(reader, start=2):
            if len(rec) != len(HEADER):
                raise ValueError(f"line {line}: expected {len(HEADER)} columns, got {len(rec)}")
            if not rec[0].isdigit() or int(rec[0]) != len(rows):
                raise ValueError(f"line {line}: window numbers must be 0, 1, 2, ... in order")
            try:
                vals = [float(x) for x in rec[1:]]
            except ValueError:
                raise ValueError(f"line {line}: a value is not a number") from None
            if any(not math.isfinite(v) or v < 0 for v in vals):
                raise ValueError(f"line {line}: values must be finite and not negative")
            rows.append(dict(zip(FEATURES, vals, strict=True)))
    if not rows:
        raise ValueError("the file has no data rows")
    return rows
