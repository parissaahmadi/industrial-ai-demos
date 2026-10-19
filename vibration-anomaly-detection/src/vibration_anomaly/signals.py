"""Synthetic vibration windows. Seeded, so every run is reproducible.

Healthy: a 1x shaft tone, a weak 2x tone and Gaussian noise. Faults are *textbook caricatures*, not physics:
  imbalance  - the 1x tone grows
  bearing    - periodic impulses at the defect frequency ring a 300 Hz resonance (raises crest factor, kurtosis, HF energy)
  looseness  - harmonics 3x and 4x plus a 0.5x sub-harmonic appear
``severity`` goes from 0 (healthy) to 1 (strong). ``speed_factor`` and ``load_factor`` model a *healthy* change of
operating condition (shaft speed; vibration amplitude with load).
"""

from __future__ import annotations

import math
import random

from .machines import FS, WINDOW, Machine

FAULTS = ("none", "imbalance", "bearing", "looseness")
A1, A2, NOISE = 1.0, 0.2, 0.15  # amplitudes of 1x and 2x, noise sigma (arbitrary units)
RESONANCE_HZ, RING_DECAY = 300.0, 60.0


def _ring(t: float) -> float:
    return math.exp(-RING_DECAY * t) * math.sin(2 * math.pi * RESONANCE_HZ * t)


def make_window(machine: Machine, rng: random.Random, index: int, fault: str = "none", severity: float = 0.0,
                speed_factor: float = 1.0, load_factor: float = 1.0) -> list[float]:  # fmt: skip
    """Window number ``index`` (time starts at index seconds, so the phase continues from window to window)."""
    if fault not in FAULTS:
        raise ValueError(f"unknown fault {fault!r}")
    if not 0.0 <= severity <= 1.0:
        raise ValueError("severity must be between 0 and 1")
    if speed_factor <= 0 or load_factor <= 0:
        raise ValueError("speed_factor and load_factor must be positive")
    f1 = machine.shaft_hz * speed_factor
    a1 = A1 * (1 + 2.5 * severity) if fault == "imbalance" else A1
    a1, a2 = a1 * load_factor, A2 * load_factor  # load scales the machine's own vibration, not the sensor noise
    t0 = index * WINDOW / FS
    out = []
    for n in range(WINDOW):
        t = t0 + n / FS
        x = a1 * math.sin(2 * math.pi * f1 * t) + a2 * math.sin(2 * math.pi * 2 * f1 * t + 0.7)
        if fault == "looseness" and severity > 0:
            x += load_factor * severity * (0.5 * math.sin(2 * math.pi * 3 * f1 * t + 1.1) + 0.4 * math.sin(2 * math.pi * 4 * f1 * t)
                             + 0.5 * math.sin(2 * math.pi * 0.5 * f1 * t + 0.3))  # fmt: skip
        x += rng.gauss(0.0, NOISE)
        out.append(x)
    if fault == "bearing" and severity > 0:
        period = 1.0 / (machine.bpfo_ratio * f1)
        k = math.ceil(t0 / period)
        while k * period < t0 + WINDOW / FS:
            start = k * period - t0
            amp = 3.0 * load_factor * severity * (1 + 0.2 * rng.gauss(0, 1))
            i0 = int(math.ceil(start * FS))
            for i in range(i0, min(WINDOW, i0 + int(0.06 * FS))):
                out[i] += amp * _ring(i / FS - start)
            k += 1
    return out
