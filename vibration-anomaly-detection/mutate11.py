"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate11.py       (RUNNER="uv run pytest -q -x" python mutate11.py to choose the test command;
                                  MUTATION_JOBS=2 parallel copies, MUTATION_TIMEOUT=240 seconds per mutant)
A mutant that makes the suite hang counts as killed. Tests run in file order with -x, and the slow demo test is last,
so most mutants die in seconds.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()
TIMEOUT = int(os.environ.get("MUTATION_TIMEOUT", "240"))
JOBS = int(os.environ.get("MUTATION_JOBS", "2"))
COPY = ["src", "tests", "pyproject.toml"]
S = "src/vibration_anomaly/"

# (file, [(old, new), ...], what the bug is)
MUTATIONS = [
    # --- fft
    (S + "fft.py", [("    if not is_pow2(n):", "    if False:")], "non power-of-two length accepted"),
    (S + "fft.py", [("return n >= 1 and n & (n - 1) == 0", "return n >= 0 and n & (n - 1) == 0")], "length 0 counts as a power of two"),
    (S + "fft.py", [("cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)", "cmath.exp(2j * math.pi * k / n) for k in range(n // 2)")], "twiddle sign flipped"),
    (S + "fft.py", [("half, step = size // 2, n // size", "half, step = size // 2, 1")], "wrong twiddle stride"),
    (S + "fft.py", [("0.5 - 0.5 * math.cos(2 * math.pi * i / n)", "0.54 - 0.46 * math.cos(2 * math.pi * i / n)")], "Hamming instead of Hann"),
    (S + "fft.py", [("math.cos(2 * math.pi * i / n) for i in range(n)]  # periodic", "math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]  # periodic")], "symmetric instead of periodic Hann"),
    # --- signals
    (S + "signals.py", [("if fault not in FAULTS:", "if False:")], "unknown fault accepted"),
    (S + "signals.py", [("if not 0.0 <= severity <= 1.0:", "if not 0.0 <= severity < 1.0:")], "severity 1 refused"),
    (S + "signals.py", [("if not 0.0 <= severity <= 1.0:", "if not 0.0 < severity <= 1.0:")], "severity 0 refused"),
    (S + "signals.py", [("if speed_factor <= 0 or load_factor <= 0:", "if speed_factor < 0 or load_factor < 0:")], "zero speed or load accepted"),
    (S + "signals.py", [("A1 * (1 + 2.5 * severity)", "A1 * (1 + 0.1 * severity)")], "imbalance far too weak"),
    (S + "signals.py", [("a1, a2 = a1 * load_factor, A2 * load_factor", "a1, a2 = a1 * load_factor, A2")], "load does not scale the 2x tone"),
    (S + "signals.py", [("x += load_factor * severity * (", "x += severity * (")], "load does not scale looseness"),
    (S + "signals.py", [("amp = 3.0 * load_factor * severity", "amp = 3.0 * severity")], "load does not scale the bearing impulses"),
    (S + "signals.py", [("period = 1.0 / (machine.bpfo_ratio * f1)", "period = 1.0 / (machine.bpfo_ratio * machine.shaft_hz)")], "bearing frequency ignores shaft speed"),
    (S + "signals.py", [("RESONANCE_HZ, RING_DECAY = 300.0, 60.0", "RESONANCE_HZ, RING_DECAY = 250.0, 60.0")], "resonance moved"),
    (S + "signals.py", [("RESONANCE_HZ, RING_DECAY = 300.0, 60.0", "RESONANCE_HZ, RING_DECAY = 300.0, 600.0")], "ringing far too short"),
    (S + "signals.py", [("i0 + int(0.06 * FS)", "i0 + int(0.005 * FS)")], "ringing cut after 5 ms"),
    (S + "signals.py", [("k = math.ceil(t0 / period)", "k = math.floor(t0 / period)")], "impulse before the window written into it"),
    (S + "signals.py", [("t0 = index * WINDOW / FS", "t0 = 0.0")], "every window starts at time zero"),
    (S + "signals.py", [("1.1) + 0.4", "1.1) + 0.9")], "looseness 3x/4x balance changed"),
    # --- features
    (S + "features.py", [("x = [v - mean for v in samples]", "x = list(samples)")], "DC offset not removed"),
    (S + "features.py", [("kurt = (sum(v**4 for v in x) / n) / (ms * ms) if ms > 0 else 0.0", "kurt = (sum(v**4 for v in x) / n) / ms if ms > 0 else 0.0")], "kurtosis normalised wrongly"),
    (S + "features.py", [("peak / rms if rms > 0 else 0.0", "peak * rms if rms > 0 else 0.0")], "crest factor wrong"),
    (S + "features.py", [("max(1, int(math.floor(lo * WINDOW / FS)))", "max(0, int(math.floor(lo * WINDOW / FS)))")], "DC bin counted in bands"),
    (S + "features.py", [("min(WINDOW // 2, int(math.ceil(hi * WINDOW / FS)))", "min(WINDOW - 1, int(math.ceil(hi * WINDOW / FS)))")], "mirror half of the spectrum counted"),
    (S + "features.py", [("return 2.0 * sum(", "return sum(")], "one-sided factor missing"),
    (S + "features.py", [("_HANN_NORM = sum(w * w for w in _HANN)", "_HANN_NORM = sum(_HANN)")], "window power normalisation wrong"),
    (S + "features.py", [('"band_1x": band_energy(spec, f1 * 0.95, f1 * 1.05)', '"band_1x": band_energy(spec, f1 * 0.95, f1 * 2.05)')], "1x band swallows 2x"),
    (S + "features.py", [('"band_1x": band_energy(spec, f1 * 0.95, f1 * 1.05)', '"band_1x": band_energy(spec, f1 * 0.9, f1 * 1.1)')], "1x band 10 % wide instead of 5 %"),
    (S + "features.py", [("band_energy(spec, f1 * 2.95, f1 * 4.05)", "band_energy(spec, f1 * 1.95, f1 * 4.05)")], "harmonic band includes 2x"),
    (S + "features.py", [(" + band_energy(spec, f1 * 0.45, f1 * 0.55)", "")], "sub-harmonic not measured"),
    (S + "features.py", [("if any(not math.isfinite(v) for v in samples):", "if False:")], "NaN accepted"),
    (S + "features.py", [("if len(samples) != WINDOW:", "if len(samples) < WINDOW:")], "longer windows accepted"),
    (S + "features.py", [("HF_BAND = (150.0, 500.0)", "HF_BAND = (150.0, 400.0)")], "high-frequency band narrowed"),
    # --- detectors
    (S + "detectors.py", [("MIN_BASELINE = 30", "MIN_BASELINE = 20")], "baseline of 20 windows accepted"),
    (S + "detectors.py", [("MAD_TO_SIGMA = 1.4826", "MAD_TO_SIGMA = 1.0")], "MAD not scaled to sigma"),
    (S + "detectors.py", [("REL_FLOOR, LOG_FLOOR = 0.02, 0.05", "REL_FLOOR, LOG_FLOOR = 0.002, 0.05")], "relative floor tiny"),
    (S + "detectors.py", [("REL_FLOOR, LOG_FLOOR = 0.02, 0.05", "REL_FLOOR, LOG_FLOOR = 0.02, 0.5")], "log floor huge"),
    (S + "detectors.py", [("floor = LOG_FLOOR if f in LOG_FEATURES else REL_FLOOR * abs(med)", "floor = REL_FLOOR * abs(med)")], "log features get a relative floor"),
    (S + "detectors.py", [('LOG_FEATURES = frozenset({"band_1x", "band_harm", "band_hf"})', 'LOG_FEATURES = frozenset({"band_1x", "band_harm"})')], "hf band scored linearly"),
    (S + "detectors.py", [("statistics.median(vals)", "statistics.mean(vals)")], "mean instead of median"),
    (S + "detectors.py", [("max(value, 1e-9)", "max(value, 1e-3)")], "log floor too high"),
    (S + "detectors.py", [("(transform(f, row[f]) - self.center[f]) / self.scale[f]", "(transform(f, row[f]) - self.center[f]) * self.scale[f]")], "z multiplies by the scale"),
    (S + "detectors.py", [("if len(rows) < MIN_BASELINE:", "if len(rows) <= MIN_BASELINE:")], "baseline boundary off by one"),
    (S + "detectors.py", [("if k < 0 or h <= 0:", "if k <= 0 or h <= 0:")], "k = 0 refused"),
    (S + "detectors.py", [("if k < 0 or h <= 0:", "if k < 0 or h < 0:")], "h = 0 accepted"),
    (S + "detectors.py", [("self.s = max(0.0, self.s + z - self.k)", "self.s = max(0.0, self.s + z + self.k)")], "cusum adds k"),
    (S + "detectors.py", [("self.s = max(0.0, self.s + z - self.k)", "self.s = self.s + z - self.k")], "cusum can go negative"),
    (S + "detectors.py", [("return self.s > self.h", "return self.s >= self.h")], "cusum boundary inclusive"),
    (S + "detectors.py", [("if not 0 < warn_z <= alarm_z:", "if not 0 < warn_z < alarm_z:")], "equal thresholds refused"),
    (S + "detectors.py", [("if not 0 < warn_z <= alarm_z:", "if not 0 <= warn_z <= alarm_z:")], "zero warn threshold accepted"),
    (S + "detectors.py", [("if not 1 <= need <= window or clear_after < 1:", "if not 1 <= need < window or clear_after < 1:")], "need = window refused"),
    (S + "detectors.py", [("if not 1 <= need <= window or clear_after < 1:", "if not 0 <= need <= window or clear_after < 1:")], "need = 0 accepted"),
    (S + "detectors.py", [("or clear_after < 1:", "or clear_after < 0:")], "clear_after = 0 accepted"),
    (S + "detectors.py", [("if drift_feature not in FEATURES:", "if False:")], "unknown drift feature accepted"),
    (S + "detectors.py", [("feature = max(zs, key=lambda f: zs[f])", "feature = min(zs, key=lambda f: zs[f])")], "reports the smallest z"),
    (S + "detectors.py", [("drift = self._cusum.update(zs[self.drift_feature])", "drift = self._cusum.update(top)")], "drift ignores the chosen feature"),
    (S + "detectors.py", [("hits_alarm = sum(v >= self.alarm_z", "hits_alarm = sum(v > self.alarm_z")], "alarm threshold exclusive"),
    (S + "detectors.py", [("hits_warn = sum(v >= self.warn_z", "hits_warn = sum(v > self.warn_z")], "warn threshold exclusive"),
    (S + "detectors.py", [("if hits_alarm >= self.need:", "if hits_alarm > self.need:")], "persistence needs one more hit"),
    (S + "detectors.py", [("else WARN if hits_warn >= self.need else OK", "else WARN if hits_warn > self.need else OK")], "warn persistence off by one"),
    (S + "detectors.py", [("self._calm = self._calm + 1 if top < self.warn_z else 0", "self._calm = self._calm + 1 if top <= self.warn_z else 0")], "calm threshold inclusive"),
    (S + "detectors.py", [("self._calm = self._calm + 1 if top < self.warn_z else 0", "self._calm = self._calm + 1 if top < self.warn_z else self._calm")], "a noisy window does not restart the calm count"),
    (S + "detectors.py", [("if self._calm >= self.clear_after:", "if self._calm > self.clear_after:")], "clearing needs one more calm window"),
    (S + "detectors.py", [("self._alarmed, self._calm = True, 0", "self._alarmed = True")], "calm count survives a re-alarm"),
    (S + "detectors.py", [("deque(maxlen=window)", "deque(maxlen=window + 1)")], "memory one window too long"),
    (S + "detectors.py", [("warn_z: float = 4.0, alarm_z: float = 6.0, window: int = 5, need: int = 3,", "warn_z: float = 3.0, alarm_z: float = 6.0, window: int = 5, need: int = 3,")], "default warn threshold 3"),
    (S + "detectors.py", [("warn_z: float = 4.0, alarm_z: float = 6.0, window: int = 5, need: int = 3,", "warn_z: float = 4.0, alarm_z: float = 4.5, window: int = 5, need: int = 3,")], "default alarm threshold 4.5"),
    (S + "detectors.py", [("warn_z: float = 4.0, alarm_z: float = 6.0, window: int = 5, need: int = 3,", "warn_z: float = 4.0, alarm_z: float = 6.0, window: int = 5, need: int = 2,")], "default persistence 2 of 5"),
    (S + "detectors.py", [("clear_after: int = 5", "clear_after: int = 2")], "default clear_after 2"),
    (S + "detectors.py", [('drift_feature: str = "rms"', 'drift_feature: str = "crest"')], "default drift feature crest"),
    (S + "detectors.py", [("def __init__(self, k: float = 1.0, h: float = 12.0)", "def __init__(self, k: float = 0.5, h: float = 12.0)")], "default cusum k 0.5"),
    (S + "detectors.py", [("def __init__(self, k: float = 1.0, h: float = 12.0)", "def __init__(self, k: float = 1.0, h: float = 6.0)")], "default cusum h 6"),
    # --- csv
    (S + "csvio.py", [("or v < 0 for v in vals", "or v <= 0 for v in vals")], "zero refused"),
    (S + "csvio.py", [("or v < 0 for v in vals", "for v in vals")], "negative values accepted"),
    (S + "csvio.py", [("if any(not math.isfinite(v)", "if any(False and not math.isfinite(v)")], "NaN and infinity accepted"),
    (S + "csvio.py", [("or int(rec[0]) != len(rows):", ":")], "window numbers not checked"),
    (S + "csvio.py", [("if not rec[0].isdigit()", "if False")], "non-numeric window number crashes"),
    (S + "csvio.py", [("if len(rec) != len(HEADER):", "if len(rec) < len(HEADER):")], "extra columns accepted"),
    (S + "csvio.py", [("enumerate(reader, start=2)", "enumerate(reader, start=1)")], "line numbers off by one"),
    (S + "csvio.py", [("    if not rows:\n", "    if False:\n")], "empty data accepted"),
    (S + "csvio.py", [("if header != HEADER:", "if header is None:")], "wrong header accepted"),
    (S + "csvio.py", [("repr(float(r[f]))", 'f"{float(r[f]):.3f}"')], "values rounded when written"),
    # --- evaluate
    (S + "evaluate.py", [("if self.fault == \"none\" or i < ONSET:", "if self.fault == \"none\" or i <= ONSET:")], "onset window already healthy"),
    (S + "evaluate.py", [("self.final * (i - ONSET + 1) / self.ramp", "self.final * (i - ONSET) / self.ramp")], "ramp starts one window late"),
    (S + "evaluate.py", [("return min(self.final, self.final * (i - ONSET + 1) / self.ramp)", "return self.final * (i - ONSET + 1) / self.ramp")], "ramp not capped"),
    (S + "evaluate.py", [("if self.ramp == 0:", "if self.ramp == 1:")], "abrupt scenarios ramp"),
    (S + "evaluate.py", [("return self.speed_factor if i >= ONSET else 1.0", "return self.speed_factor if i > ONSET else 1.0")], "speed change one window late"),
    (S + "evaluate.py", [("return self.load_factor if i >= ONSET else 1.0", "return self.load_factor")], "load change from the start"),
    (S + "evaluate.py", [("self.statuses[BASELINE_WINDOWS:ONSET]", "self.statuses[BASELINE_WINDOWS + 1:ONSET]")], "first post-baseline window not counted"),
    (S + "evaluate.py", [("self.statuses[BASELINE_WINDOWS:ONSET]", "self.statuses[:ONSET]")], "baseline windows counted as false alarms"),
    (S + "evaluate.py", [("self.statuses[BASELINE_WINDOWS:ONSET]", "self.statuses[BASELINE_WINDOWS:ONSET + 1]")], "onset window counted as false alarm"),
    (S + "evaluate.py", [("sum(s.level == ALARM for s in self.statuses[ONSET:])", "sum(s.level == ALARM for s in self.statuses[ONSET + 1:])")], "onset window not counted after onset"),
    (S + "evaluate.py", [("return None if a is None else a - ONSET", "return None if a is None else a")], "delay counted from zero"),
    (S + "evaluate.py", [('order = {"OK": 0, WARN: 1, ALARM: 2}', 'order = {"OK": 0, WARN: 2, ALARM: 1}')], "levels ordered wrongly"),
    (S + "evaluate.py", [("delays[len(delays) // 2] if delays", "delays[0] if delays")], "minimum instead of median"),
    (S + "evaluate.py", [("baseline = Baseline.fit(rows[:BASELINE_WINDOWS])", "baseline = Baseline.fit(rows[:BASELINE_WINDOWS + 50])")], "baseline includes post-onset windows"),
    (S + "evaluate.py", [("det = Detector(baseline, **detector_args)", "det = Detector(baseline)")], "detector arguments ignored"),
    (S + "evaluate.py", [("BASELINE_WINDOWS = 60", "BASELINE_WINDOWS = 40")], "baseline length changed"),
    (S + "evaluate.py", [("TOTAL = 220", "TOTAL = 160")], "runs shortened"),
    (S + "evaluate.py", [('rng = random.Random(f"{machine.asset_id}/{scenario.name}/{seed}")', 'rng = random.Random(f"{machine.asset_id}/{scenario.name}")')], "seed ignored"),
    # --- cli
    (S + "cli.py", [("round((v - lo) / (hi - lo) * (len(_BARS) - 1))", "int((v - lo) / (hi - lo) * (len(_BARS) - 1))")], "sparkline truncates"),
    (S + "cli.py", [("v = max(values[i:i + group])", "v = min(values[i:i + group])")], "sparkline shows the smallest of a group"),
    (S + "cli.py", [("max(0, min(len(_BARS) - 1,", "max(0, min(len(_BARS),")], "sparkline can index past the bars"),
    (S + "cli.py", [("return 2 if any(s.level == ALARM for s in statuses) else 0", "return 0")], "detect always exits 0"),
    (S + "cli.py", [('print(f"error: {e}", file=sys.stderr)\n        return 1', 'print(f"error: {e}", file=sys.stderr)\n        return 0')], "errors exit 0"),
    (S + "cli.py", [("if i == 0 or s.level != statuses[i - 1].level:", "if True:")], "every window printed"),
    (S + "cli.py", [("Baseline.fit(rows[: args.baseline_windows])", "Baseline.fit(rows)")], "--baseline-windows ignored"),
    (S + "cli.py", [("Detector(baseline, warn_z=args.warn_z, alarm_z=args.alarm_z)", "Detector(baseline)")], "--warn-z/--alarm-z ignored"),
    (S + "cli.py", [("def _transitions(statuses) -> list[tuple[int, str]]:\n    out, prev = [], None", "def _transitions(statuses) -> list[tuple[int, str]]:\n    out, prev = [], OK")], "first window never listed"),
    (S + "cli.py", [('p.add_argument("--alarm-z", type=float, default=6.0)', 'p.add_argument("--alarm-z", type=float, default=5.0)')], "default --alarm-z"),
    (S + "cli.py", [('p.add_argument("--final", type=float, default=1.0)', 'p.add_argument("--final", type=float, default=0.5)')], "default --final"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return 124


def one(args):
    i, (rel, edits, why), tmp = args
    tree = Path(tmp) / f"m{i}"
    for name in COPY:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            tree.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, tree / name)
    target = tree / rel
    text = target.read_text()
    for old, new in edits:
        if old not in text:
            return i, "setup", f"pattern not found in {rel}: {old[:70]!r}", why
        text = text.replace(old, new, 1)
    target.write_text(text)
    killed = run(tree) != 0
    shutil.rmtree(tree, ignore_errors=True)
    return i, "killed" if killed else "SURVIVED", rel, why


def main() -> int:
    survived, errors = [], 0
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(JOBS) as pool:
        for i, state, rel, why in pool.map(one, [(i, m, tmp) for i, m in enumerate(MUTATIONS, 1)]):
            print(f"{i:3} {state:8} {Path(rel).name:14} {why}", flush=True)
            if state == "SURVIVED":
                survived.append(why)
            elif state == "setup":
                errors += 1
    print(f"\n{len(MUTATIONS) - len(survived) - errors}/{len(MUTATIONS)} mutations killed")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
