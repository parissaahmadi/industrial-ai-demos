import math

import pytest
from helpers import healthy_rows, row
from vibration_anomaly.detectors import (
    ALARM,
    LOG_FEATURES,
    MIN_BASELINE,
    OK,
    WARN,
    Baseline,
    Cusum,
    Detector,
    transform,
)
from vibration_anomaly.features import FEATURES


# ------------------------------------------------------------------ baseline
def test_baseline_needs_enough_windows():
    assert MIN_BASELINE == 30
    with pytest.raises(ValueError):
        Baseline.fit(healthy_rows(29))
    assert Baseline.fit(healthy_rows(30)).n == 30


def test_center_is_the_median_and_z_is_zero_there():
    b = Baseline.fit(healthy_rows())
    z = b.z({f: (10 ** b.center[f] if f in LOG_FEATURES else b.center[f]) for f in FEATURES})
    assert all(abs(v) < 1e-9 for v in z.values())


def test_a_few_odd_windows_do_not_distort_the_baseline():
    clean = Baseline.fit(healthy_rows(60))
    rows = healthy_rows(60)
    for i in (3, 17, 40):
        rows[i] = row(rms=50.0, band_hf=9.0, kurtosis=40.0)
    dirty = Baseline.fit(rows)
    for f in FEATURES:
        assert abs(dirty.center[f] - clean.center[f]) < 0.3 * clean.scale[f] + 1e-9
        assert dirty.scale[f] < 1.6 * clean.scale[f]


def test_mad_is_scaled_to_sigma():
    rows = [row(rms=float(v)) for v in range(-14, 16)]  # 30 values 0.. centred; MAD of 1..30 style data
    rows = [row(rms=100.0 + v) for v in range(30)]
    b = Baseline.fit(rows)
    assert abs(b.center["rms"] - 114.5) < 1e-9
    assert abs(b.scale["rms"] - 7.5 * 1.4826) < 1e-9  # median |dev| of 0..29 about 14.5 is 7.5


def test_a_perfectly_steady_baseline_still_has_a_scale():
    b = Baseline.fit([row() for _ in range(40)])
    assert all(b.scale[f] > 0 for f in FEATURES)
    assert abs(b.scale["rms"] - 0.02) < 1e-12  # 2 % of the median
    assert abs(b.scale["band_hf"] - 0.05) < 1e-12  # 0.05 decades on the log scale
    assert abs(b.z(row(rms=1.1))["rms"] - 5.0) < 1e-9  # a 10 % rise is 5 floors


def test_energies_are_scored_on_a_log_scale():
    assert LOG_FEATURES == {"band_1x", "band_harm", "band_hf"}
    assert transform("band_hf", 100.0) == 2.0 and transform("rms", 100.0) == 100.0
    assert math.isfinite(transform("band_hf", 0.0)) and transform("band_hf", -3.0) == -9.0
    b = Baseline.fit(healthy_rows())
    assert b.z(row(band_hf=0.02))["band_hf"] > 10  # ten times the energy is far above the baseline


def test_z_is_signed():
    b = Baseline.fit(healthy_rows())
    assert b.z(row(rms=0.5))["rms"] < -5 and b.z(row(rms=2.0))["rms"] > 5


# ------------------------------------------------------------------ cusum
def test_cusum_validation():
    for k, h in [(-0.1, 5), (1, 0), (1, -2)]:
        with pytest.raises(ValueError):
            Cusum(k, h)
    Cusum(0, 1)


def test_cusum_accumulates_only_the_excess_and_resets_at_zero():
    c = Cusum(k=1.0, h=5.0)
    assert [c.update(z) for z in (0, 1, 2)] == [False, False, False] and c.s == 1.0
    assert c.update(-50) is False and c.s == 0.0  # never below zero


def test_cusum_threshold_is_strict():
    c = Cusum(k=0.0, h=3.0)
    assert [c.update(1.0) for _ in range(4)] == [False, False, False, True]  # 3.0 is not > 3.0
    assert c.s == 4.0


def test_cusum_catches_a_creep_that_never_crosses_the_z_threshold():
    c = Cusum()
    flagged = [c.update(2.5) for _ in range(20)]
    assert flagged.index(True) == 8  # 1.5 per window: 1.5*9 = 13.5 > 12


# ------------------------------------------------------------------ detector
def make(**kw):
    return Detector(Baseline.fit(healthy_rows()), **kw)


def feed(det, values, feature="rms"):
    """Rows whose rms z equals the given numbers (steady baseline: scale known)."""
    b = det.baseline
    return [det.update(row(**{feature: b.center[feature] + v * b.scale[feature]})) for v in values]


def test_quiet_machine_stays_ok():
    det = make()
    assert {s.level for s in feed(det, [0, 1, -1, 0.5] * 10)} == {OK}


def test_one_spike_is_not_enough():
    det = make()
    assert [s.level for s in feed(det, [0, 20, 0, 0, 0])] == [OK] * 5


def test_warn_needs_three_of_the_last_five():
    det = make()
    levels = [s.level for s in feed(det, [5, 0, 5, 0, 5])]
    assert levels == [OK, OK, OK, OK, WARN]


def test_alarm_needs_three_windows_above_the_alarm_threshold():
    det = make()
    levels = [s.level for s in feed(det, [7, 7, 5])]
    assert levels == [OK, OK, WARN]  # 5 is a warn, not an alarm
    det2 = make()
    assert [s.level for s in feed(det2, [7, 7, 7])] == [OK, OK, ALARM]


def exact(**kw):
    """A baseline with centre 0 and scale 1 for every feature, so a rms of v is exactly z = v."""
    return Detector(Baseline({f: 0.0 for f in FEATURES}, {f: 1.0 for f in FEATURES}, 60), **kw)


def push(det, values):
    return [det.update(row(rms=float(v), band_1x=1.0, band_harm=1.0, band_hf=1.0, crest=0, kurtosis=0)) for v in values]


def test_thresholds_are_inclusive():
    for z, want in [(6.0, ALARM), (5.999, WARN), (4.0, WARN), (3.999, OK)]:
        assert [s.level for s in push(exact(warn_z=4.0, alarm_z=6.0), [z] * 3)][-1] == want, z


def test_the_window_forgets_old_values():
    det = make()
    levels = [s.level for s in feed(det, [7, 7, 0, 0, 0, 0, 0, 7])]
    assert levels[-1] == OK  # only 2 of the last 5 are high


def test_alarm_latches_until_the_evidence_is_forgotten_and_the_calm_has_lasted():
    # window 5, need 3, clear_after 3: the three high windows stay in the memory for two more windows, then
    # three calm windows must follow
    levels = [s.level for s in push(exact(clear_after=3), [7, 7, 7, 0, 0, 0, 0, 0, 0])]
    assert levels == [OK, OK, ALARM, ALARM, ALARM, ALARM, ALARM, OK, OK]


def test_a_noisy_window_restarts_the_calm_count():
    levels = [s.level for s in push(exact(clear_after=3), [7, 7, 7, 0, 0, 0, 0, 5, 0, 0, 0])]
    # without the 5 the alarm would clear at index 7; the 5 restarts the count, so it clears three windows later
    assert levels[7:10] == [ALARM] * 3 and levels[10] != ALARM


def test_calm_means_below_the_warn_threshold():
    base = [7, 7, 7, 0, 0]
    assert push(exact(clear_after=2, warn_z=4.0), base + [3.9, 3.9, 3.9])[-1].level != ALARM
    assert push(exact(clear_after=2, warn_z=4.0), base + [4.0, 4.0, 4.0])[-1].level == ALARM


def test_clear_after_one_clears_at_the_first_calm_window_after_forgetting():
    levels = [s.level for s in push(exact(clear_after=1), [7, 7, 7, 0, 0, 0])]
    assert levels[-1] != ALARM and levels[-2] == ALARM


def test_status_names_the_worst_feature_and_its_z():
    det = make()
    s = det.update(row(band_hf=0.2))
    assert s.feature == "band_hf" and s.z > 10
    s = det.update(row(kurtosis=8.0))
    assert s.feature == "kurtosis"


def test_drift_flag_comes_from_the_chosen_feature():
    det = make(drift_feature="rms")
    flags = [det.update(row(rms=1.0 + 0.05 * (1 + i * 0.0) * 3)).drift for i in range(30)]  # z=7.5 steady
    assert any(flags)
    det = make(drift_feature="kurtosis")
    assert not any(det.update(row(rms=1.15)).drift for _ in range(30))


@pytest.mark.parametrize("kw", [dict(warn_z=0), dict(warn_z=-1), dict(warn_z=7, alarm_z=6), dict(need=0),
                                dict(need=6, window=5), dict(clear_after=0), dict(drift_feature="speed")])  # fmt: skip
def test_bad_parameters(kw):
    with pytest.raises(ValueError):
        make(**kw)


def test_equal_warn_and_alarm_and_minimal_window_are_allowed():
    make(warn_z=5, alarm_z=5)
    d = make(window=1, need=1)
    assert feed(d, [7])[0].level == ALARM


def test_a_re_alarm_restarts_the_calm_count():
    # need 2 of 5: the alarm is raised at index 2, the memory forgets it at index 5 (calm 1), a new high window at
    # index 6 raises it again and must reset the calm count, so three more calm windows are needed (clear at 9)
    levels = [s.level for s in push(exact(need=2, clear_after=3), [7, 0, 7, 0, 0, 0, 7, 0, 0, 0])]
    assert levels[2:9] == [ALARM] * 7 and levels[9] != ALARM


def test_documented_defaults():
    import inspect

    params = inspect.signature(Detector).parameters
    got = {k: v.default for k, v in params.items() if k != "baseline"}
    assert got == {"warn_z": 4.0, "alarm_z": 6.0, "window": 5, "need": 3, "clear_after": 5, "drift_feature": "rms"}
    c = inspect.signature(Cusum).parameters
    assert (c["k"].default, c["h"].default) == (1.0, 12.0)


def test_with_the_defaults_an_alarm_clears_after_seven_calm_windows():
    """The README states this number: 3 windows until the high ones leave the 5-window memory, then 5 calm windows,
    the first of which is the 3rd after the last high one."""
    det = exact()
    levels = [s.level for s in push(det, [7, 7, 7] + [0] * 9)]
    assert levels[2:9] == [ALARM] * 7 and levels[9] != ALARM
