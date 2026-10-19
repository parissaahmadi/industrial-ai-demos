# vibration-anomaly-detection

Explainable anomaly detection on **synthetic** vibration data, the kind of check a predictive-maintenance system
runs on a pump or fan: per-window features, a robust baseline learned from the machine's own healthy behaviour,
persistence rules so one odd window does not raise an alarm, and a CUSUM for slow drift. Standard library only
(including the FFT), no machine learning.

> **Demo data is fictional and generated.** Machines (`PMP-0001` Vortex VX-200 pump, `FAN-0003` Helix HX-9 fan,
> `CMP-0002` Quanta QC-5 compressor), their speeds and "bearing frequencies" are invented. The signals are textbook
> caricatures of faults (see `signals.py`), **not** physical models and **not** recordings. Nothing here comes from a
> real site, company or machine.

**This is a technique demo, not a validated product.** No threshold was tuned on real machines, and the
evaluation below is on data this repository generated itself. Read [What the evaluation does and does not
show](#what-the-evaluation-does-and-does-not-show) before drawing any conclusion from the numbers.

## How it works

1. **Window → features** (`features.py`). One window is 1 s at 1024 Hz (1024 samples; one FFT bin = 1 Hz). Six cheap
   features a maintenance engineer can read: `rms`, `crest` (peak / rms), `kurtosis`, and three spectral band
   energies: `band_1x` (shaft speed ±5 %), `band_harm` (3x-4x and the 0.5x sub-harmonic) and `band_hf` (150-500 Hz,
   where the fictional bearing resonance rings). The mean is removed first (sensor offset); the spectrum uses a Hann
   window.
2. **Baseline** (`detectors.py`). From the first 60 windows (assumed healthy) it stores the **median** and the
   **MAD** (scaled by 1.4826) per feature, so a few odd windows in the baseline barely move it. Energies are scored on a
   log scale. A floor on the scale (2 % of the median, or 0.05 decades) keeps a perfectly steady baseline from making
   everything look infinitely unusual.
3. **Score**: robust z = (value − median) / scale, per feature. The status reports the **largest** z and which feature
   it is (`worst feature`). That is a pointer for a human, not a diagnosis.
4. **Persistence**: `WARN` needs 3 of the last 5 windows at z ≥ 4; `ALARM` needs 3 of the last 5 at z ≥ 6. An alarm
   **latches**: with the defaults it clears after 7 consecutive calm windows (z < 4) following the last high one
   (3 windows until the high ones leave the 5-window memory, then 5 calm windows counted from the 3rd; the test
   `test_with_the_defaults_an_alarm_clears_after_seven_calm_windows` pins it).
5. **Drift**: a one-sided CUSUM on the `rms` z (k = 1, h = 12) flags a machine that creeps away from its baseline
   without any single window being dramatic.

## Run it

```bash
uv sync
uv run vibration-anomaly demo            # about 30 s: the FFT is plain Python
uv run vibration-anomaly simulate --fault looseness --machine FAN-0003 --ramp 40 --out loose.csv
uv run vibration-anomaly detect loose.csv      # exit code 2 if an ALARM was raised, 1 on a bad file
```

`detect` reads a CSV of **features** (not raw samples): `window,rms,crest,kurtosis,band_1x,band_harm,band_hf`, one row
per window, windows numbered 0, 1, 2, ... in order, values finite and not negative. A bad file is an error with a line
number, never silently skipped.

## The scripted demo (real output)

`uv run vibration-anomaly demo`, output of this repository's code, unedited (everything is seeded, so it is the same
every run):

```text
== 1. one run: Vortex VX-200 pump (PMP-0001), bearing fault from window 100, severity 0 -> 1 over 60 windows
  baseline windows 0-59; one character = 4 windows (largest robust z; scale 0..12)
  z     |▁▁▁▁▁▁▁▁▁▂▁▂▁▁▁▁▁▁▂▁ ▁▁▁▁▂▅████████████████████████████|
  mark  |               B         F                             |   B = baseline ends, F = fault starts
  window   0  OK     z=  0.4  worst feature: crest      true severity 0.00
  window 107  WARN   z=  7.5  worst feature: band_hf    true severity 0.13
  window 108  ALARM  z=  9.0  worst feature: band_hf    true severity 0.15
  CUSUM drift flag first raised at window 115 (true severity 0.27)

== 2. what the detector does across scenarios (PMP-0001, seeds 0-4; synthetic data)
  scenario                     truth   alarmed  median delay  FA before onset  alarm windows after onset
  healthy                      healthy 0/5      -             0                0
  imbalance, gradual           fault   5/5      5 windows     0                575
  bearing, gradual             fault   5/5      8 windows     0                559
  looseness, gradual           fault   5/5      8 windows     0                561
  bearing, abrupt and mild     fault   5/5      2 windows     0                590
  bearing, barely there        fault   3/5      88 windows    0                88
  healthy, load +40 %          healthy 5/5      2 windows     0                590
  (FA = ALARM windows between the end of the baseline and the onset; the last column is out of 600 windows)
```

A session with files:

```text
$ uv run vibration-anomaly simulate --fault looseness --machine FAN-0003 --ramp 40 --out loose.csv
wrote 220 windows to loose.csv (fault onset at window 100)
$ uv run vibration-anomaly detect loose.csv
baseline: first 60 windows; 220 windows scored
  window    0  OK     z=   1.7  worst feature: crest
  window  104  WARN   z=   7.4  worst feature: band_harm
  window  106  ALARM  z=   9.7  worst feature: band_harm
  drift (CUSUM on rms) first flagged at window 119
(exit code 2)
$ head -3 loose.csv
window,rms,crest,kurtosis,band_1x,band_harm,band_hf
0,0.7453747070666491,2.200084604598506,1.7439918584634835,0.5109024463345533,0.0006529243927074595,0.014941966012090984
1,0.7350594643907293,2.063507325058457,1.695123625328694,0.4986935500361729,0.0010950735727383843,0.015112184266499434
$ uv run vibration-anomaly simulate --fault none --out ok.csv
wrote 220 windows to ok.csv (healthy throughout)
$ uv run vibration-anomaly detect ok.csv
baseline: first 60 windows; 220 windows scored
  window    0  OK     z=   1.1  worst feature: band_harm
  no drift flagged
(exit code 0)
$ uv run vibration-anomaly detect nothere.csv
error: [Errno 2] No such file or directory: 'nothere.csv'
(exit code 1)
```

## What the evaluation does and does not show

Reading the table above, in this order:

* **Fault scenarios are found, quickly, on this data.** Imbalance, bearing and looseness ramps are alarmed within 5-8
  windows of the onset, at a true severity of 0.10-0.17 (measured over the 5 seeds; section 1 shows 0.15 for one bearing run).
  "Delay" is counted **in windows**; how long that is in wall-clock time depends on how often a real system takes a
  window.
* **Zero false alarms before the onset is optimistic.** The simulated noise is stationary and Gaussian and the
  baseline comes from the same generator. Real machines have warm-up, start/stop, process changes and sensor
  dropouts. Do not read "0" as a false-alarm rate.
* **`healthy, load +40 %` alarms in 5 of 5 runs within about two windows.** A healthy machine under more load looks
  exactly like a fault to this detector, because the baseline knows one operating condition only. This is the most
  important limitation, and a test (`test_known_limitation_a_healthy_load_increase_looks_like_a_fault`) keeps it
  visible. The usual remedy (one baseline per operating condition, selected from speed/load signals) is **not
  implemented**.
* **A barely-there bearing fault (severity 0.1) is caught in 3 of 5 runs**, after a median delay of 88 windows. That
  is where this detector's sensitivity ends on this data.
* Only 5 seeds per scenario, one machine. The numbers show behaviour, not statistics.
* The faults are made by the same code that defines the features (`band_hf` is where the generator rings its
  resonance). Detecting them is therefore close to circular. What the exercise *does* test is the machinery around
  the features: robust baseline, persistence, latching, drift, and honest reporting of where it fails.

## Design decisions and limits (read before reusing)

* **No raw-sample input.** The CLI works on feature rows; computing features from a real accelerometer stream
  (sampling rate, anti-aliasing, units, sensor placement, ISO-style velocity bands) is out of scope.
* **Baseline = the first N windows of the file**, assumed healthy. Nothing checks that assumption.
* **Thresholds (4, 6, 3 of 5, k = 1, h = 12) are illustrative**, chosen on synthetic data.
* **Scores are one-sided and the bands are fixed.** Only a *rise* in a feature is flagged, and `band_1x`/`band_harm`
  sit at fixed multiples of the nominal shaft speed. A 10 % speed change moves the 1x tone out of its band and
  nothing alarms (a test, `test_known_limitation_a_speed_change_goes_unnoticed`, keeps that visible); a fault that
  *removes* vibration would not be flagged either.
* **Pure-Python signal processing**: feature extraction (radix-2 FFT, 1024 points) takes about 2 ms per window; fine for a demo, replace it with `numpy`/`scipy`
  for anything real. It is checked against a naive DFT in the tests.
* **One feature file = one machine.** No fleet handling, no persistence of detector state between runs.
* No alert delivery, no CMMS integration (see `cmms-mcp-server` for that kind of thing).

## Testing

```bash
uv run pytest -q
uv run ruff check
python mutate11.py            # RUNNER="uv run pytest -q -x" python mutate11.py to pick the test command
```

* **138 tests**: FFT against a naive DFT, signal recipes (each fault raises the feature it should, load and
  speed behave as documented), feature maths on known signals (sine rms/crest/kurtosis, tone energy A²/2, band
  edges, DC removal), baseline robustness, persistence and latching boundaries, CUSUM, strict CSV reading, scenario
  arithmetic, simulations, the CLI, and the demo.
* **Mutation check** (`mutate11.py`): 113 deliberate bugs applied one at a time to a copy of the code
  (boundaries, removed checks, wrong constants, wrong defaults...), run on two copies in parallel. **113/113 killed**.
  The first run killed 100 of 113; the 13 survivors pointed at missing tests (load scaling of the 2x tone, looseness and bearing impulses, bearing frequency following shaft speed, the 3x/4x mix, the minimum window length message, calm-count reset on a re-alarm, the documented defaults, sparkline rounding, \"only changes are printed\"), and those tests were added; one survivor (baseline length) needed a direct test of how many windows the baseline uses.
* Flakiness: the whole suite (about 35 s) ran 3 times in a row without a failure; nothing in it uses the clock, the network or threads, and every random source is seeded.

### What was *not* verified

* The test run used a small pytest stand-in because the build sandbox has no package index (`pytest` and `ruff`
  could not be installed). The tests are plain pytest style, but **`uv run pytest` and `uv run ruff check` have not
  been run by the author**: run them once.
* Only Python 3.13 was available (`requires-python = ">=3.11"` is declared; CI runs 3.11 and 3.13).
* `uv sync` was not run, so there is no `uv.lock`; commit the one your first sync creates.
* Real accelerometer data, real fault signatures and real operating-condition handling were never involved.

## Layout

```text
src/vibration_anomaly/
  machines.py   three fictional machines, sampling rate, window length
  fft.py        radix-2 FFT, naive DFT (tests only), Hann window
  signals.py    synthetic windows: healthy + three fault recipes, speed and load factors
  features.py   rms, crest, kurtosis, three band energies
  detectors.py  Baseline (median/MAD), Detector (persistence, latching), Cusum
  csvio.py      strict feature-file reader/writer
  evaluate.py   scenarios, runs, false alarms and detection delay
  cli.py        demo, simulate, detect
tests/          138 tests (test_slow_demo.py runs last: it takes ~30 s)
mutate11.py     mutation check
```

License: MIT (put your name in `LICENSE`).
