"""Per-window features. All are cheap and explainable, which is the point: a maintenance engineer can read them."""

from __future__ import annotations

import math

from .fft import fft, hann
from .machines import FS, WINDOW, Machine

FEATURES = ("rms", "crest", "kurtosis", "band_1x", "band_harm", "band_hf")
HF_BAND = (150.0, 500.0)  # Hz, around the fictional 300 Hz bearing resonance
_HANN = hann(WINDOW)
_HANN_NORM = sum(w * w for w in _HANN)


def band_energy(spectrum: list[complex], lo: float, hi: float) -> float:
    """Mean-square amplitude in [lo, hi] Hz, from a one-sided view of the Hann-windowed spectrum."""
    a, b = max(1, int(math.floor(lo * WINDOW / FS))), min(WINDOW // 2, int(math.ceil(hi * WINDOW / FS)))
    return 2.0 * sum(abs(spectrum[k]) ** 2 for k in range(a, b + 1)) / (WINDOW * _HANN_NORM)


def extract(samples: list[float], machine: Machine) -> dict[str, float]:
    if len(samples) != WINDOW:
        raise ValueError(f"expected {WINDOW} samples, got {len(samples)}")
    if any(not math.isfinite(v) for v in samples):
        raise ValueError("samples contain NaN or infinity")
    n = len(samples)
    mean = sum(samples) / n
    x = [v - mean for v in samples]  # remove DC (sensor offset) before anything else
    ms = sum(v * v for v in x) / n
    rms = math.sqrt(ms)
    peak = max(abs(v) for v in x)
    kurt = (sum(v**4 for v in x) / n) / (ms * ms) if ms > 0 else 0.0
    spec = fft([v * w for v, w in zip(x, _HANN, strict=True)])
    f1 = machine.shaft_hz
    return {
        "rms": rms,
        "crest": peak / rms if rms > 0 else 0.0,
        "kurtosis": kurt,
        "band_1x": band_energy(spec, f1 * 0.95, f1 * 1.05),
        "band_harm": band_energy(spec, f1 * 2.95, f1 * 4.05) + band_energy(spec, f1 * 0.45, f1 * 0.55),
        "band_hf": band_energy(spec, *HF_BAND),
    }
