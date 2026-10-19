import math
import random

from vibration_anomaly.features import FEATURES
from vibration_anomaly.machines import FS, MACHINES, WINDOW

PUMP = MACHINES["PMP-0001"]


def sine(freq, amp=1.0, phase=0.0, n=WINDOW, offset=0.0):
    return [offset + amp * math.sin(2 * math.pi * freq * i / FS + phase) for i in range(n)]


def healthy_rows(n=60, seed=1, jitter=0.05):
    """Feature rows hovering around fixed values (no signal synthesis needed)."""
    rng = random.Random(seed)
    base = {"rms": 1.0, "crest": 3.0, "kurtosis": 3.0, "band_1x": 0.5, "band_harm": 0.01, "band_hf": 0.002}
    return [{f: base[f] * (1 + jitter * rng.gauss(0, 1)) for f in FEATURES} for _ in range(n)]


def row(**over):
    r = {"rms": 1.0, "crest": 3.0, "kurtosis": 3.0, "band_1x": 0.5, "band_harm": 0.01, "band_hf": 0.002}
    r.update(over)
    return r
