"""Runs scenarios on synthetic data and measures false alarms and detection delay."""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import lru_cache

from .detectors import ALARM, WARN, Baseline, Detector, Status
from .features import extract
from .machines import MACHINES, Machine
from .signals import make_window

BASELINE_WINDOWS = 60
ONSET = 100
TOTAL = 220


@dataclass(frozen=True)
class Scenario:
    name: str
    fault: str = "none"
    ramp: int = 60  # windows the severity takes to go from 0 to ``final``; 0 = abrupt
    final: float = 1.0
    speed_factor: float = 1.0  # applied from the onset on (a healthy machine running at another speed)
    load_factor: float = 1.0  # applied from the onset on (a healthy machine under more load: all vibration scales)

    @property
    def is_fault(self) -> bool:
        return self.fault != "none"

    def severity(self, i: int) -> float:
        if self.fault == "none" or i < ONSET:
            return 0.0
        if self.ramp == 0:
            return self.final
        return min(self.final, self.final * (i - ONSET + 1) / self.ramp)

    def speed(self, i: int) -> float:
        return self.speed_factor if i >= ONSET else 1.0

    def load(self, i: int) -> float:
        return self.load_factor if i >= ONSET else 1.0


SCENARIOS = (
    Scenario("healthy"),
    Scenario("imbalance, gradual", "imbalance"),
    Scenario("bearing, gradual", "bearing"),
    Scenario("looseness, gradual", "looseness"),
    Scenario("bearing, abrupt and mild", "bearing", ramp=0, final=0.3),
    Scenario("bearing, barely there", "bearing", ramp=60, final=0.1),
    Scenario("healthy, load +40 %", load_factor=1.4),
)


@dataclass(frozen=True)
class Run:
    scenario: Scenario
    machine: str
    seed: int
    statuses: tuple[Status, ...]

    def first(self, level: str, start: int = 0) -> int | None:
        order = {"OK": 0, WARN: 1, ALARM: 2}
        for i in range(start, len(self.statuses)):
            if order[self.statuses[i].level] >= order[level]:
                return i
        return None

    @property
    def false_alarm_windows(self) -> int:
        """Windows flagged ALARM before the scenario's onset (after the baseline was learned)."""
        return sum(s.level == ALARM for s in self.statuses[BASELINE_WINDOWS:ONSET])

    @property
    def alarm_windows_after_onset(self) -> int:
        return sum(s.level == ALARM for s in self.statuses[ONSET:])

    @property
    def alarm_window(self) -> int | None:
        return self.first(ALARM, ONSET)

    @property
    def delay(self) -> int | None:
        a = self.alarm_window
        return None if a is None else a - ONSET

    @property
    def severity_at_alarm(self) -> float | None:
        a = self.alarm_window
        return None if a is None else self.scenario.severity(a)


@lru_cache(maxsize=64)
def _simulate(machine: Machine, scenario: Scenario, seed: int) -> tuple[dict[str, float], ...]:
    rng = random.Random(f"{machine.asset_id}/{scenario.name}/{seed}")
    return tuple(extract(make_window(machine, rng, i, scenario.fault, scenario.severity(i), scenario.speed(i),
                                     scenario.load(i)), machine)
                 for i in range(TOTAL))  # fmt: skip


def simulate(machine: Machine, scenario: Scenario, seed: int) -> list[dict[str, float]]:
    """Feature rows of one synthetic run (cached: the same arguments give the same rows; do not edit them)."""
    return list(_simulate(machine, scenario, seed))


def detect(rows: list[dict[str, float]], **detector_args) -> list[Status]:
    baseline = Baseline.fit(rows[:BASELINE_WINDOWS])
    det = Detector(baseline, **detector_args)
    return [det.update(r) for r in rows]  # the baseline windows are scored too, so false alarms there would show


def run(machine_id: str, scenario: Scenario, seed: int, **detector_args) -> Run:
    machine = MACHINES[machine_id]
    return Run(scenario, machine_id, seed, tuple(detect(simulate(machine, scenario, seed), **detector_args)))


def summarize(machine_id: str, scenario: Scenario, seeds: range | list[int], **detector_args) -> dict:
    runs = [run(machine_id, scenario, sd, **detector_args) for sd in seeds]
    delays = sorted(r.delay for r in runs if r.delay is not None)
    return {
        "scenario": scenario.name,
        "is_fault": scenario.is_fault,
        "runs": len(runs),
        "alarmed": sum(r.alarm_window is not None for r in runs),
        "median_delay": delays[len(delays) // 2] if delays else None,
        "false_alarm_windows": sum(r.false_alarm_windows for r in runs),  # before the onset
        "alarm_windows_after_onset": sum(r.alarm_windows_after_onset for r in runs),
    }
