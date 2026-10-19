"""Baseline + robust z-scores + persistence, and a CUSUM for slow drift.

Idea: learn what *this* machine looks like when healthy (median and MAD per feature, which a few odd windows
cannot distort), score every new window by how many robust standard deviations it is above that, and only raise
a level when several recent windows agree. Nothing here is tuned on real machines.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass

from .features import FEATURES

MIN_BASELINE = 30
LOG_FEATURES = frozenset({"band_1x", "band_harm", "band_hf"})  # energies are skewed: score them on a log scale
MAD_TO_SIGMA = 1.4826
REL_FLOOR, LOG_FLOOR = 0.02, 0.05  # a perfectly steady baseline must not make everything look infinitely unusual
OK, WARN, ALARM = "OK", "WARN", "ALARM"


def transform(name: str, value: float) -> float:
    return math.log10(max(value, 1e-9)) if name in LOG_FEATURES else value


class Baseline:
    def __init__(self, center: dict[str, float], scale: dict[str, float], n: int) -> None:
        self.center, self.scale, self.n = center, scale, n

    @classmethod
    def fit(cls, rows: list[dict[str, float]]) -> Baseline:
        if len(rows) < MIN_BASELINE:
            raise ValueError(f"need at least {MIN_BASELINE} healthy windows to learn a baseline, got {len(rows)}")
        center, scale = {}, {}
        for f in FEATURES:
            vals = [transform(f, r[f]) for r in rows]
            med = statistics.median(vals)
            mad = statistics.median(abs(v - med) for v in vals) * MAD_TO_SIGMA
            floor = LOG_FLOOR if f in LOG_FEATURES else REL_FLOOR * abs(med)
            center[f], scale[f] = med, max(mad, floor, 1e-12)
        return cls(center, scale, len(rows))

    def z(self, row: dict[str, float]) -> dict[str, float]:
        return {f: (transform(f, row[f]) - self.center[f]) / self.scale[f] for f in FEATURES}


@dataclass(frozen=True)
class Status:
    level: str
    z: float  # largest robust z among the features
    feature: str  # which feature it is
    drift: bool  # CUSUM says the machine is creeping away from its baseline


class Cusum:
    """One-sided CUSUM on a z-score: sums the part of each z above ``k`` and alarms above ``h``."""

    def __init__(self, k: float = 1.0, h: float = 12.0) -> None:
        if k < 0 or h <= 0:
            raise ValueError("k must be >= 0 and h > 0")
        self.k, self.h, self.s = k, h, 0.0

    def update(self, z: float) -> bool:
        self.s = max(0.0, self.s + z - self.k)
        return self.s > self.h


class Detector:
    def __init__(self, baseline: Baseline, warn_z: float = 4.0, alarm_z: float = 6.0, window: int = 5, need: int = 3,
                 clear_after: int = 5, drift_feature: str = "rms") -> None:  # fmt: skip
        if not 0 < warn_z <= alarm_z:
            raise ValueError("need 0 < warn_z <= alarm_z")
        if not 1 <= need <= window or clear_after < 1:
            raise ValueError("need 1 <= need <= window and clear_after >= 1")
        if drift_feature not in FEATURES:
            raise ValueError(f"unknown feature {drift_feature!r}")
        self.baseline, self.warn_z, self.alarm_z = baseline, warn_z, alarm_z
        self.need, self.clear_after, self.drift_feature = need, clear_after, drift_feature
        self._recent: deque[float] = deque(maxlen=window)
        self._calm = 0
        self._alarmed = False
        self._cusum = Cusum()

    def update(self, row: dict[str, float]) -> Status:
        zs = self.baseline.z(row)
        feature = max(zs, key=lambda f: zs[f])
        top = zs[feature]
        self._recent.append(top)
        drift = self._cusum.update(zs[self.drift_feature])
        hits_alarm = sum(v >= self.alarm_z for v in self._recent)
        hits_warn = sum(v >= self.warn_z for v in self._recent)
        if hits_alarm >= self.need:
            self._alarmed, self._calm = True, 0
        elif self._alarmed:
            self._calm = self._calm + 1 if top < self.warn_z else 0
            if self._calm >= self.clear_after:
                self._alarmed = False
        level = ALARM if self._alarmed else WARN if hits_warn >= self.need else OK
        return Status(level, top, feature, drift)
