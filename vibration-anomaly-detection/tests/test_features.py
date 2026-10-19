import math

import pytest
from helpers import PUMP, sine
from vibration_anomaly.features import FEATURES, HF_BAND, band_energy, extract
from vibration_anomaly.machines import WINDOW

F1 = PUMP.shaft_hz


def test_sine_statistics():
    r = extract(sine(100.0, 2.0), PUMP)
    assert abs(r["rms"] - 2 / math.sqrt(2)) < 1e-9
    assert abs(r["crest"] - math.sqrt(2)) < 0.01
    assert abs(r["kurtosis"] - 1.5) < 0.01  # kurtosis of a sine wave


def test_gaussian_like_noise_has_kurtosis_near_three():
    import random

    rng = random.Random(1)
    r = extract([rng.gauss(0, 1) for _ in range(WINDOW)], PUMP)
    assert 2.6 < r["kurtosis"] < 3.4


def test_dc_offset_is_removed():
    a, b = extract(sine(100.0, 1.0), PUMP), extract(sine(100.0, 1.0, offset=50.0), PUMP)
    for f in FEATURES:
        assert abs(a[f] - b[f]) < 1e-6 * max(1, abs(a[f]))


def test_tone_energy_is_amplitude_squared_over_two():
    r = extract(sine(F1, 3.0), PUMP)
    assert abs(r["band_1x"] - 4.5) / 4.5 < 0.05
    assert r["band_harm"] < 1e-3 * r["band_1x"] and r["band_hf"] < 1e-3 * r["band_1x"]


def test_each_band_hears_its_own_tone_only():
    lo, hi = HF_BAND
    assert extract(sine(300.0), PUMP)["band_hf"] > 0.4
    assert extract(sine(300.0), PUMP)["band_1x"] < 1e-3
    assert extract(sine(3 * F1), PUMP)["band_harm"] > 0.4
    assert extract(sine(0.5 * F1 + 0.0), PUMP)["band_harm"] > 0.3
    assert extract(sine(2 * F1), PUMP)["band_harm"] < 1e-3  # 2x is deliberately in no band
    assert (lo, hi) == (150.0, 500.0)


def test_band_energy_ignores_dc_and_respects_edges():
    from vibration_anomaly.fft import fft, hann

    spec = fft([v * w for v, w in zip(sine(100.0, 1.0), hann(WINDOW), strict=True)])
    inside = band_energy(spec, 99, 101)
    assert abs(inside - 0.5) < 0.03
    assert band_energy(spec, 200, 300) < 1e-6
    assert band_energy(spec, 0, 0.4) < 1e-9  # starts at bin 1: DC is never counted
    assert band_energy(spec, 400, 5000) < 1e-9  # clipped at Nyquist, no index error
    dc = fft([v * w for v, w in zip([5.0] * WINDOW, hann(WINDOW), strict=True)])
    assert abs(dc[0]) > 1000 and band_energy(dc, 0, 0.9) == band_energy(dc, 1, 1)  # a band starting at 0 Hz starts at bin 1


def test_all_zero_window_does_not_divide_by_zero():
    r = extract([0.0] * WINDOW, PUMP)
    assert r["rms"] == 0 and r["crest"] == 0 and r["kurtosis"] == 0


@pytest.mark.parametrize("n", [0, 1, WINDOW - 1, WINDOW + 1])
def test_wrong_length(n):
    with pytest.raises(ValueError) as e:
        extract([0.0] * n, PUMP)
    assert f"expected {WINDOW} samples, got {n}" in str(e.value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_samples_are_refused(bad):
    x = sine(100.0)
    x[10] = bad
    with pytest.raises(ValueError):
        extract(x, PUMP)


def test_feature_names_are_stable():
    assert FEATURES == ("rms", "crest", "kurtosis", "band_1x", "band_harm", "band_hf")
    assert set(extract(sine(100.0), PUMP)) == set(FEATURES)
