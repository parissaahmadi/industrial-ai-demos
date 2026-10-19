"""Fictional machines. Nothing here describes a real product; the numbers only make the signals plausible."""

from __future__ import annotations

from dataclasses import dataclass

FS = 1024  # sampling rate, Hz
WINDOW = 1024  # samples per analysis window: 1 s, so one FFT bin is 1 Hz


@dataclass(frozen=True)
class Machine:
    asset_id: str
    name: str
    rpm: float
    bpfo_ratio: float  # bearing outer-race defect frequency as a multiple of shaft speed (fictional)

    @property
    def shaft_hz(self) -> float:
        return self.rpm / 60.0


MACHINES = {
    "PMP-0001": Machine("PMP-0001", "Vortex VX-200 pump", 1480, 3.58),
    "FAN-0003": Machine("FAN-0003", "Helix HX-9 fan", 980, 3.05),
    "CMP-0002": Machine("CMP-0002", "Quanta QC-5 compressor", 2960, 4.12),
}
