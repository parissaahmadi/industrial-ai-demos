import math
import random

import pytest
from helpers import PUMP
from vibration_anomaly.features import extract
from vibration_anomaly.machines import FS, MACHINES, WINDOW
from vibration_anomaly.signals import A1, A2, FAULTS, NOISE, make_window


def win(**kw):
    rng = random.Random(kw.pop("seed", 1))
    return make_window(PUMP, rng, kw.pop("index", 0), **kw)


def test_shape_and_determinism():
    a, b = win(), win()
    assert len(a) == WINDOW and a == b
    assert win(seed=2) != a


def test_healthy_rms_matches_the_recipe():
    rows = [extract(win(seed=s), PUMP) for s in range(20)]
    mean_rms = sum(r["rms"] for r in rows) / 20
    expected = math.sqrt((A1**2 + A2**2) / 2 + NOISE**2)
    assert abs(mean_rms - expected) < 0.01


def test_machines_have_plausible_numbers():
    for m in MACHINES.values():
        assert 5 < m.shaft_hz < 60 and 2 < m.bpfo_ratio < 5
    assert abs(PUMP.shaft_hz - 1480 / 60) < 1e-12
    assert FS == WINDOW  # one second per window, so one FFT bin is one hertz


def test_severity_zero_is_healthy_for_every_fault():
    base = win()
    for fault in FAULTS:
        assert win(fault=fault, severity=0.0) == base


def test_imbalance_grows_the_1x_tone_only():
    h, f = extract(win(), PUMP), extract(win(fault="imbalance", severity=1.0), PUMP)
    assert f["band_1x"] > 10 * h["band_1x"]
    assert f["band_hf"] < 2 * h["band_hf"] and f["rms"] > 1.5 * h["rms"]


def test_bearing_fault_shows_in_the_high_frequency_band_and_in_impulsiveness():
    h = extract(win(), PUMP)
    f = extract(win(fault="bearing", severity=1.0), PUMP)
    assert f["band_hf"] > 30 * h["band_hf"]
    assert f["kurtosis"] > h["kurtosis"] + 0.5 and f["crest"] > h["crest"]


def test_looseness_adds_harmonics():
    h, f = extract(win(), PUMP), extract(win(fault="looseness", severity=1.0), PUMP)
    assert f["band_harm"] > 20 * h["band_harm"]
    assert abs(f["band_1x"] / h["band_1x"] - 1) < 0.3


def test_severity_is_monotonic_in_its_feature():
    vals = [extract(win(fault="bearing", severity=s), PUMP)["band_hf"] for s in (0.1, 0.4, 0.7, 1.0)]
    assert vals == sorted(vals) and vals[-1] > 5 * vals[0]


def test_load_scales_the_machine_not_the_sensor_noise():
    quiet = [extract(win(load_factor=1.0, seed=s), PUMP) for s in range(10)]
    loaded = [extract(win(load_factor=1.4, seed=s), PUMP) for s in range(10)]
    ratio_1x = sum(r["band_1x"] for r in loaded) / sum(r["band_1x"] for r in quiet)
    assert abs(ratio_1x - 1.4**2) < 0.05  # tone energy scales with the square of the amplitude
    noise_floor = [extract(win(load_factor=1.4, seed=s), PUMP)["band_hf"] for s in range(10)]
    base_floor = [extract(win(seed=s), PUMP)["band_hf"] for s in range(10)]
    assert abs(sum(noise_floor) / sum(base_floor) - 1) < 0.2  # sensor noise does not move


def test_speed_factor_moves_the_shaft_tone_out_of_its_band():
    h, f = extract(win(), PUMP), extract(win(speed_factor=1.1), PUMP)
    assert f["band_1x"] < 0.2 * h["band_1x"]  # only leakage is left


def test_phase_continues_between_windows():
    rng = random.Random(0)
    a = make_window(PUMP, rng, 0)
    rng = random.Random(0)
    b = make_window(PUMP, rng, 1)
    assert a != b  # time starts at index seconds


@pytest.mark.parametrize("kw", [dict(fault="rotor"), dict(severity=-0.1), dict(severity=1.01),
                                dict(speed_factor=0), dict(load_factor=-1)])  # fmt: skip
def test_bad_arguments(kw):
    with pytest.raises(ValueError):
        win(**kw)


def test_boundaries_of_severity_are_allowed():
    win(fault="bearing", severity=1.0)
    win(fault="bearing", severity=0.0)


# ------------------------------------------------------------------ operating condition and fault recipes
def test_load_scales_both_tones_in_the_total_rms():
    from vibration_anomaly import signals

    expected = math.sqrt((signals.A1**2 + signals.A2**2) / 2 * 1.4**2 + signals.NOISE**2)
    got = sum(extract(win(load_factor=1.4, seed=s), PUMP)["rms"] for s in range(20)) / 20
    assert abs(got - expected) < 0.01


def test_load_scales_looseness_and_bearing_impulses():
    for fault, feature in (("looseness", "band_harm"), ("bearing", "band_hf")):
        one = sum(extract(win(fault=fault, severity=1.0, seed=s), PUMP)[feature] for s in range(6))
        loaded = sum(extract(win(fault=fault, severity=1.0, load_factor=1.4, seed=s), PUMP)[feature] for s in range(6))
        assert 1.6 < loaded / one < 2.4, fault  # energy follows the square of the amplitude (1.4**2 = 1.96)


def test_looseness_mixes_3x_and_4x_in_the_documented_ratio():
    from vibration_anomaly.features import band_energy
    from vibration_anomaly.fft import fft, hann

    spec = fft([v * w for v, w in zip(win(fault="looseness", severity=1.0), hann(WINDOW), strict=True)])
    f1 = PUMP.shaft_hz
    ratio = band_energy(spec, 2.95 * f1, 3.05 * f1) / band_energy(spec, 3.95 * f1, 4.05 * f1)
    assert abs(ratio - (0.5 / 0.4) ** 2) < 0.2


def _impulse_count(monkeypatch, speed):
    from vibration_anomaly import signals

    for name, value in (("A1", 0.0), ("A2", 0.0), ("NOISE", 0.0), ("RING_DECAY", 1500.0)):
        monkeypatch.setattr(signals, name, value)
    x = win(fault="bearing", severity=1.0, speed_factor=speed)
    count, quiet = 0, 99
    for v in x:
        if abs(v) > 0.5:
            count += quiet >= 3  # a new impulse starts after at least three quiet samples
            quiet = 0
        else:
            quiet += 1
    return count


def test_bearing_impulses_follow_the_shaft_speed(monkeypatch):
    slow, fast = _impulse_count(monkeypatch, 1.0), _impulse_count(monkeypatch, 1.3)
    expected = PUMP.bpfo_ratio * PUMP.shaft_hz
    assert abs(slow - expected) < 8 and abs(fast - 1.3 * expected) < 10 and fast - slow > 15
