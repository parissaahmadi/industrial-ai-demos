"""Fictional plant data: made-up assets, made-up readings, made-up people. Nothing here comes from a
real site. Deterministic, so demos and tests print the same thing every time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import IngestRequest
from .transcribe import make_fakevox

BASE = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)

# (minutes after BASE, asset, kind, severity, message, tags)
_ROWS = [
    (0, "PMP-0001", "event", "info", "Vortex VX-200 pump started, discharge pressure 6.2 bar", ["startup"]),
    (12, "PMP-0001", "alarm", "warning", "Bearing temperature above 75 C on pump drive end", ["bearing", "temperature"]),
    (25, "PMP-0001", "alarm", "critical", "Bearing temperature above 90 C, pump tripped", ["bearing", "temperature", "trip"]),
    (31, "PMP-0001", "note", "info", "دمای یاتاقان پمپ از حد مجاز بیشتر شد و پمپ متوقف شد", ["یاتاقان", "دما"]),
    (47, "CMP-0002", "alarm", "warning", "Helix HX-40 compressor: abnormal vibration on stage 2", ["vibration"]),
    (55, "CMP-0002", "note", "info", "ارتعاش غیرعادی در کمپرسور مرحله‌ی دوم بررسی شد", ["ارتعاش"]),
    (80, "FAN-0003", "alarm", "warning", "Nimbus NB-9 cooling fan: motor current 12% above rated", ["motor", "current"]),
    (95, "FAN-0003", "note", "info", "موتور فن خنك‌كننده بازرسی شد؛ لرزش قابل توجه نبود", ["موتور", "فن"]),
    (120, "GBX-0004", "alarm", "critical", "Ironwood IW-7 gearbox: oil leak detected at output seal", ["oil", "leak", "seal"]),
    (126, "GBX-0004", "note", "info", "نشت روغن از آب‌بند خروجی گیربکس مشاهده شد، سطح روغن پایین است", ["نشت", "روغن"]),
    (140, "GBX-0004", "event", "info", "Oil top-up 2 L completed, leak rate being monitored", ["oil", "maintenance"]),
    (170, "PMP-0001", "event", "info", "Pump restarted after bearing replacement", ["startup", "maintenance"]),
    (185, "VLV-0005", "alarm", "warning", "Quanta QT-3 control valve: position deviation 8%", ["valve", "position"]),
    (200, "VLV-0005", "event", "info", "Valve positioner recalibrated", ["valve", "calibration"]),
    (230, "CMP-0002", "alarm", "critical", "Compressor discharge temperature high-high", ["temperature", "trip"]),
    (236, "CMP-0002", "note", "info", "فشار خروجی کمپرسور افت کرد و فیلتر هوا عوض شد", ["فشار", "فیلتر"]),
    (260, "FAN-0003", "event", "info", "Fan belt tension adjusted", ["maintenance"]),
    (300, "PMP-0001", "alarm", "warning", "Seal flush flow low", ["seal", "flow"]),
    (310, "GBX-0004", "alarm", "warning", "Gearbox oil temperature trending up", ["oil", "temperature"]),
    (330, "VLV-0005", "note", "info", "Operator note: valve stem packing looks dry, schedule lubrication", ["valve", "packing"]),
]


def sample_events() -> list[IngestRequest]:
    out = []
    for i, (minutes, asset, kind, sev, msg, tags) in enumerate(_ROWS, 1):
        when = (BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(IngestRequest(occurred_at=when, asset_id=asset, kind=kind, severity=sev, message=msg, tags=tags,
                                 source="sim-plant", source_ref=f"sim-{i:04d}"))  # fmt: skip
    return out


# (index into sample_events, audio). Index is 0-based.
def sample_recordings() -> list[tuple[int, bytes]]:
    return [
        (3, make_fakevox([("دمای یاتاقان بالا رفت", 0.93), ("پمپ را متوقف کردم", 0.91)], "fa")),
        (9, make_fakevox([("نشت روغن از آب‌بند", 0.88), ("سطح روغن پایین است", 0.58)], "fa")),
        (12, make_fakevox([("valve positioner needs recalibration", 0.95)], "en", fail_attempts=2)),
        (18, make_fakevox([("oil temperature rising slowly", 0.9), ("check cooler fouling", 0.9)], "en")),
        (19, b"FAKEVOX1\n{broken"),
    ]
