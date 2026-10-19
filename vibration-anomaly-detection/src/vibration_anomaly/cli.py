"""Command line: demo, simulate, detect."""

from __future__ import annotations

import argparse
import sys

from . import evaluate as ev
from .csvio import read_features, write_features
from .detectors import ALARM, Baseline, Detector
from .machines import MACHINES
from .signals import FAULTS

_BARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], lo: float = 0.0, hi: float = 12.0, group: int = 4) -> str:
    """One character per ``group`` values (the largest of them), scaled between lo and hi."""
    chars = []
    for i in range(0, len(values), group):
        v = max(values[i:i + group])
        chars.append(_BARS[max(0, min(len(_BARS) - 1, round((v - lo) / (hi - lo) * (len(_BARS) - 1))))])
    return "".join(chars)


def _transitions(statuses) -> list[tuple[int, str]]:
    out, prev = [], None
    for i, s in enumerate(statuses):
        if s.level != prev:
            out.append((i, s.level))
            prev = s.level
    return out


def cmd_simulate(args) -> int:
    sc = ev.Scenario(f"{args.fault}", args.fault, ramp=args.ramp, final=args.final)
    rows = ev.simulate(MACHINES[args.machine], sc, args.seed)
    write_features(rows, args.out)
    what = f"fault onset at window {ev.ONSET}" if sc.is_fault else "healthy throughout"
    print(f"wrote {len(rows)} windows to {args.out} ({what})")
    return 0


def cmd_detect(args) -> int:
    try:
        rows = read_features(args.csv)
        baseline = Baseline.fit(rows[: args.baseline_windows])
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    det = Detector(baseline, warn_z=args.warn_z, alarm_z=args.alarm_z)
    statuses = [det.update(r) for r in rows]
    print(f"baseline: first {baseline.n} windows; {len(rows)} windows scored")
    for i, s in enumerate(statuses):
        if i == 0 or s.level != statuses[i - 1].level:
            print(f"  window {i:4}  {s.level:5}  z={s.z:6.1f}  worst feature: {s.feature}")
    drift = next((i for i, s in enumerate(statuses) if s.drift), None)
    print(f"  drift (CUSUM on rms) first flagged at window {drift}" if drift is not None else "  no drift flagged")
    return 2 if any(s.level == ALARM for s in statuses) else 0


def cmd_demo(args) -> int:
    machine = MACHINES["PMP-0001"]
    sc = next(s for s in ev.SCENARIOS if s.name == "bearing, gradual")
    print(f"== 1. one run: {machine.name} ({machine.asset_id}), bearing fault from window {ev.ONSET}, severity 0 -> 1 over {sc.ramp} windows")
    r = ev.run(machine.asset_id, sc, seed=0)
    print(f"  baseline windows 0-{ev.BASELINE_WINDOWS - 1}; one character = 4 windows (largest robust z; scale 0..12)")
    print("  z     |" + sparkline([s.z for s in r.statuses]) + "|")
    marks = [" "] * (ev.TOTAL // 4)
    marks[ev.BASELINE_WINDOWS // 4] = "B"
    marks[ev.ONSET // 4] = "F"
    print("  mark  |" + "".join(marks) + "|   B = baseline ends, F = fault starts")
    for i, level in _transitions(r.statuses):
        s = r.statuses[i]
        print(f"  window {i:3}  {level:5}  z={s.z:5.1f}  worst feature: {s.feature:9}  true severity {sc.severity(i):.2f}")
    drift = next(i for i, s in enumerate(r.statuses) if s.drift)
    print(f"  CUSUM drift flag first raised at window {drift} (true severity {sc.severity(drift):.2f})")

    print("\n== 2. what the detector does across scenarios (PMP-0001, seeds 0-4; synthetic data)")
    print(f"  {'scenario':28} {'truth':7} {'alarmed':8} {'median delay':13} {'FA before onset':16} {'alarm windows after onset'}")
    for s in ev.SCENARIOS:
        m = ev.summarize("PMP-0001", s, range(5))
        delay = "-" if m["median_delay"] is None else f"{m['median_delay']} windows"
        print(f"  {m['scenario']:28} {'fault' if m['is_fault'] else 'healthy':7} {m['alarmed']}/{m['runs']:<6} {delay:13} {m['false_alarm_windows']:<16} {m['alarm_windows_after_onset']}")
    print("  (FA = ALARM windows between the end of the baseline and the onset; the last column is out of "
          f"{5 * (ev.TOTAL - ev.ONSET)} windows)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vibration-anomaly", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="scripted, offline, seeded").set_defaults(fn=cmd_demo)
    p = sub.add_parser("simulate", help="write a synthetic feature file")
    p.add_argument("--machine", choices=sorted(MACHINES), default="PMP-0001")
    p.add_argument("--fault", choices=FAULTS, default="bearing")
    p.add_argument("--ramp", type=int, default=60, help="windows from severity 0 to --final (0 = abrupt)")
    p.add_argument("--final", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser("detect", help="score a feature file (exit code 2 if an ALARM was raised)")
    p.add_argument("csv")
    p.add_argument("--baseline-windows", type=int, default=ev.BASELINE_WINDOWS)
    p.add_argument("--warn-z", type=float, default=4.0)
    p.add_argument("--alarm-z", type=float, default=6.0)
    p.set_defaults(fn=cmd_detect)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)
