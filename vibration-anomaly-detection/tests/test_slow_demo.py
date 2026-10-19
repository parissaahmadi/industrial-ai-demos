"""The scripted demo, in its own file: it is the slowest test (~30 s of pure-Python signal processing), so it runs last."""

import io
import sys

from vibration_anomaly import cli


class Capture:
    def __enter__(self):
        self.out, self._o = io.StringIO(), sys.stdout
        sys.stdout = self.out
        return self

    def __exit__(self, *a):
        sys.stdout = self._o


def test_demo_runs_and_tells_the_truth_about_its_limits():
    with Capture() as c:
        assert cli.main(["demo"]) == 0
    text = c.out.getvalue()
    for needle in ["bearing fault from window 100", "baseline windows 0-59", "worst feature: band_hf",
                   "CUSUM drift flag first raised", "healthy                      healthy 0/5",
                   "healthy, load +40 %          healthy 5/5", "bearing, barely there        fault   3/5"]:  # fmt: skip
        assert needle in text, needle
    lines = {ln.split("  ")[1].strip(): ln for ln in text.splitlines() if ln.startswith("  ") and "/5" in ln}
    assert all(ln.split()[-2] == "0" for ln in lines.values())  # no false alarms before any onset
