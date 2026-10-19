import pytest
from vibration_anomaly import evaluate as ev
from vibration_anomaly.detectors import ALARM, OK, WARN, Status
from vibration_anomaly.machines import MACHINES

PUMP = MACHINES["PMP-0001"]


def st(*levels):
    return tuple(Status(lv, 0.0, "rms", False) for lv in levels)


# ------------------------------------------------------------------ scenario arithmetic
def test_severity_is_zero_before_the_onset_and_ramps_from_it():
    s = ev.Scenario("x", "bearing", ramp=10, final=1.0)
    assert s.severity(ev.ONSET - 1) == 0.0
    assert abs(s.severity(ev.ONSET) - 0.1) < 1e-12  # the onset window already has the first step
    assert abs(s.severity(ev.ONSET + 4) - 0.5) < 1e-12
    assert s.severity(ev.ONSET + 9) == 1.0 and s.severity(ev.ONSET + 50) == 1.0


def test_final_severity_caps_the_ramp():
    s = ev.Scenario("x", "bearing", ramp=10, final=0.3)
    assert abs(s.severity(ev.ONSET + 4) - 0.15) < 1e-12 and s.severity(ev.ONSET + 20) == 0.3


def test_abrupt_scenarios_jump_to_final():
    s = ev.Scenario("x", "bearing", ramp=0, final=0.3)
    assert s.severity(ev.ONSET - 1) == 0.0 and s.severity(ev.ONSET) == 0.3


def test_healthy_scenarios_have_no_severity_whatever_the_settings():
    s = ev.Scenario("x", "none", ramp=0, final=1.0)
    assert not s.is_fault and s.severity(ev.TOTAL - 1) == 0.0
    assert ev.Scenario("y", "bearing").is_fault


def test_speed_and_load_apply_from_the_onset():
    s = ev.Scenario("x", speed_factor=1.1, load_factor=1.4)
    assert (s.speed(ev.ONSET - 1), s.load(ev.ONSET - 1)) == (1.0, 1.0)
    assert (s.speed(ev.ONSET), s.load(ev.ONSET)) == (1.1, 1.4)


def test_scenario_catalogue():
    names = [s.name for s in ev.SCENARIOS]
    assert len(names) == len(set(names)) == 7
    assert sum(s.is_fault for s in ev.SCENARIOS) == 5
    assert ev.BASELINE_WINDOWS < ev.ONSET < ev.TOTAL


# ------------------------------------------------------------------ run properties (hand-made statuses)
def make_run(levels, scenario=None):
    return ev.Run(scenario or ev.Scenario("x", "bearing"), "PMP-0001", 0, st(*levels))


def test_first_alarm_and_delay_are_counted_from_the_onset():
    levels = [OK] * ev.TOTAL
    levels[ev.ONSET + 7] = ALARM
    levels[ev.ONSET + 3] = WARN
    r = make_run(levels)
    assert r.alarm_window == ev.ONSET + 7 and r.delay == 7
    assert r.first(WARN, ev.ONSET) == ev.ONSET + 3
    assert r.first(ALARM, ev.ONSET + 8) is None


def test_an_alarm_before_the_onset_is_not_a_detection_but_a_false_alarm():
    levels = [OK] * ev.TOTAL
    levels[ev.BASELINE_WINDOWS] = ALARM  # first window after the baseline
    levels[ev.BASELINE_WINDOWS - 1] = ALARM  # inside the baseline: not counted
    levels[ev.ONSET - 1] = ALARM
    levels[ev.ONSET] = ALARM  # not counted as false alarm (onset itself)
    r = make_run(levels)
    assert r.false_alarm_windows == 2
    assert r.alarm_window == ev.ONSET and r.delay == 0
    assert r.alarm_windows_after_onset == 1


def test_never_alarmed():
    r = make_run([OK] * ev.TOTAL)
    assert r.alarm_window is None and r.delay is None and r.severity_at_alarm is None


def test_severity_at_alarm_uses_the_scenario():
    levels = [OK] * ev.TOTAL
    levels[ev.ONSET + 29] = ALARM
    r = make_run(levels, ev.Scenario("x", "bearing", ramp=60))
    assert abs(r.severity_at_alarm - 0.5) < 1e-12


# ------------------------------------------------------------------ real simulations (kept small)
def test_simulation_is_deterministic_and_cached():
    a = ev.simulate(PUMP, ev.SCENARIOS[0], 0)
    b = ev.simulate(PUMP, ev.SCENARIOS[0], 0)
    assert a == b and len(a) == ev.TOTAL
    assert ev.simulate(PUMP, ev.SCENARIOS[0], 1) != a
    a.pop()  # the caller's list is its own
    assert len(ev.simulate(PUMP, ev.SCENARIOS[0], 0)) == ev.TOTAL


def test_a_healthy_machine_raises_no_alarm():
    r = ev.run("PMP-0001", ev.SCENARIOS[0], 0)
    assert r.alarm_window is None and r.false_alarm_windows == 0
    assert all(s.level == OK for s in r.statuses)


@pytest.mark.parametrize("fault,feature", [("imbalance", ("band_1x", "rms")), ("bearing", ("band_hf",)), ("looseness", ("band_harm",))])
def test_each_fault_is_found_and_blamed_on_a_matching_feature(fault, feature):
    # "worst feature" is the largest z, not a diagnosis: for imbalance rms and the 1x band rise together
    r = ev.run("PMP-0001", ev.Scenario(fault, fault, ramp=40), 0)
    assert r.alarm_window is not None and 0 <= r.delay < 25
    assert r.statuses[r.alarm_window].feature in feature
    assert r.false_alarm_windows == 0


def test_known_limitation_a_healthy_load_increase_looks_like_a_fault():
    """Documented in the README: the baseline knows one operating condition only."""
    r = ev.run("PMP-0001", next(s for s in ev.SCENARIOS if s.name == "healthy, load +40 %"), 0)
    assert r.alarm_window is not None and r.delay <= 5


def test_summarize_fields_on_two_seeds():
    m = ev.summarize("PMP-0001", ev.Scenario("imbalance", "imbalance", ramp=40), [0, 1])
    assert m["runs"] == 2 and m["alarmed"] == 2 and m["is_fault"] is True
    assert m["median_delay"] is not None and m["false_alarm_windows"] == 0
    assert m["alarm_windows_after_onset"] > 100
    h = ev.summarize("PMP-0001", ev.SCENARIOS[0], [0])
    assert h["alarmed"] == 0 and h["median_delay"] is None and h["is_fault"] is False


def test_median_delay_is_the_middle_value(monkeypatch):
    delays = iter([9, 1, 5])

    class Fake:
        def __init__(self):
            self.delay = next(delays)
            self.alarm_window = 1
            self.false_alarm_windows = 0
            self.alarm_windows_after_onset = 0

    monkeypatch.setattr(ev, "run", lambda *a, **k: Fake())
    assert ev.summarize("PMP-0001", ev.SCENARIOS[1], range(3))["median_delay"] == 5


def test_detector_arguments_are_passed_through():
    strict = ev.run("PMP-0001", ev.Scenario("imbalance", "imbalance", ramp=40), 0, alarm_z=1e6, warn_z=1e5)
    assert strict.alarm_window is None


def test_the_baseline_is_exactly_the_first_windows(monkeypatch):
    seen = []
    real = ev.Baseline.fit

    def spy(rows):
        seen.append(len(rows))
        return real(rows)

    monkeypatch.setattr(ev.Baseline, "fit", staticmethod(spy))
    ev.detect(ev.simulate(PUMP, ev.SCENARIOS[0], 0))
    assert seen == [ev.BASELINE_WINDOWS] == [60]


def test_known_limitation_a_speed_change_goes_unnoticed():
    """Documented in the README: scores are one-sided and the bands are fixed, so a 10 % speed change (the 1x tone
    leaves its band: band_1x falls) raises nothing."""
    r = ev.run("PMP-0001", ev.Scenario("speed +10 %", speed_factor=1.1), 0)
    assert r.alarm_window is None and all(s.level == OK for s in r.statuses)
