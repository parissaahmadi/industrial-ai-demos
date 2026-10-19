"""Transcription engines.

``Transcriber`` is the seam: anything with ``transcribe(audio: bytes) -> Transcript`` can be plugged
in. This repository ships only ``MockTranscriber``; it does **not** do speech recognition. It reads a
fictional container ("FAKEVOX") whose payload is a JSON script, which lets tests and demos exercise
every path of the pipeline (success, low confidence, silence, transient failure, permanent failure)
deterministically, offline.

    b"FAKEVOX1\\n" + json: {"lang": "fa", "segments": [{"start": 0.0, "end": 2.1, "text": "...", "conf": 0.93}],
                           "fail_attempts": 2, "fail": "permanent" | "transient"}

``fail_attempts: n`` makes the first ``n`` calls for this audio fail transiently (the mock counts
calls per audio hash), ``fail: "permanent"`` always fails permanently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

MAGIC = b"FAKEVOX1\n"
MEDIA_TYPE = "audio/x-fakevox"


class TransientError(Exception):
    """Worth retrying later (engine busy, timeout)."""


class PermanentError(Exception):
    """Retrying cannot help (unreadable or unsupported audio)."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    conf: float


@dataclass(frozen=True)
class Transcript:
    language: str
    segments: tuple[Segment, ...]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def confidence(self) -> float | None:
        if not self.segments:
            return None
        return round(sum(s.conf for s in self.segments) / len(self.segments), 3)


class Transcriber(Protocol):
    def transcribe(self, audio: bytes) -> Transcript: ...


def make_fakevox(segments: list[tuple[str, float]], lang: str = "fa", **options) -> bytes:
    """Build a FAKEVOX file. ``segments`` is ``[(text, confidence), ...]``; 2 s per segment."""
    segs = [{"start": 2.0 * i, "end": 2.0 * i + 2.0, "text": t, "conf": c} for i, (t, c) in enumerate(segments)]
    return MAGIC + json.dumps({"lang": lang, "segments": segs, **options}, ensure_ascii=False).encode()


def check_container(audio: bytes) -> dict:
    """Parse and validate a FAKEVOX container; raises ``PermanentError`` when unusable."""
    if not audio.startswith(MAGIC):
        raise PermanentError("not a FAKEVOX file")
    try:
        doc = json.loads(audio[len(MAGIC):].decode("utf-8"))
        lang = doc["lang"]
        segs = [Segment(float(s["start"]), float(s["end"]), str(s["text"]), float(s["conf"])) for s in doc["segments"]]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise PermanentError("FAKEVOX payload is damaged") from None
    if lang not in ("fa", "en") or any(not 0.0 <= s.conf <= 1.0 or s.end < s.start for s in segs):
        raise PermanentError("FAKEVOX payload has unsupported values")
    doc["_parsed"] = Transcript(lang, tuple(segs))
    return doc


class MockTranscriber:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def transcribe(self, audio: bytes) -> Transcript:
        doc = check_container(audio)
        key = hashlib.sha256(audio).hexdigest()
        self.calls[key] = n = self.calls.get(key, 0) + 1
        if doc.get("fail") == "permanent":
            raise PermanentError("engine rejected the audio")
        if n <= int(doc.get("fail_attempts", 0)):
            raise TransientError("engine busy (simulated)")
        return doc["_parsed"]
