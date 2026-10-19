"""The archive's rules. Everything the HTTP layer and the CLI do goes through ``Archive``.

Design points
-------------
* One write transaction at a time (``BEGIN IMMEDIATE`` + a process lock), so the audit chain stays
  linear even with a second process (the worker) on the same database file.
* Every state change writes its audit row in the *same* transaction; if the change rolls back, so does
  its audit row. Denied attempts are audited in their own small transaction.
* The audit trail records *that* something happened and fingerprints (SHA-256) of text, never the text
  of messages or transcripts, so redacting an event really removes its content.
* The transcriber is called with no transaction open; a lease + attempt number guards the result
  against a worker that was presumed dead and replaced.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from . import audit as audit_log
from .db import hash_token
from .errors import (
    Forbidden,
    Invalid,
    NotFound,
    RecordingConflict,
    SourceRefConflict,
    StaleVersion,
    TooLarge,
    Unauthorized,
    UnsupportedMedia,
    WrongState,
)
from .models import CorrectRequest, IngestRequest, RedactRequest, fmt_timestamp, parse_timestamp
from .query import COLUMNS, build_match
from .textnorm import normalize
from .transcribe import MAGIC, MEDIA_TYPE, MockTranscriber, PermanentError, Transcriber, TransientError

log = logging.getLogger("event_archive")

PERMS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "operator": frozenset({"read", "ingest", "attach", "correct", "retry", "work"}),
    "admin": frozenset({"read", "ingest", "attach", "correct", "retry", "work", "redact", "audit"}),
    "auditor": frozenset({"audit"}),
}
LOW_CONFIDENCE = 0.85
LOW_SEGMENT = 0.6
MAX_LIMIT = 100
MAX_OFFSET = 1000
REDACTED_TEXT = "[redacted]"


class Principal(NamedTuple):
    username: str
    display_name: str
    role: str


SYSTEM = Principal("system:worker", "Transcription worker", "system")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Archive:
    def __init__(
        self,
        db: sqlite3.Connection,
        clock: Callable[[], datetime],
        transcriber: Transcriber | None = None,
        *,
        max_attempts: int = 4,
        backoff_base: int = 30,
        backoff_cap: int = 900,
        lease_seconds: int = 120,
        max_audio: int = 1_000_000,
        future_skew: int = 300,
    ) -> None:
        self.db, self.clock = db, clock
        self.transcriber = transcriber or MockTranscriber()
        self.max_attempts, self.backoff_base, self.backoff_cap = max_attempts, backoff_base, backoff_cap
        self.lease_seconds, self.max_audio, self.future_skew = lease_seconds, max_audio, future_skew
        self._lock = threading.RLock()
        self._before_finish: Callable[[], None] | None = None  # test seam: runs between transcribe and finish

    # ------------------------------------------------------------------ plumbing
    def _now(self) -> str:
        return fmt_timestamp(self.clock())

    @contextmanager
    def _tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _audit(self, p: Principal, action: str, target: str, **detail) -> None:
        audit_log.append(self.db, self._now(), p.username, p.role, action, target, detail)

    def _require(self, p: Principal, perm: str) -> None:
        if perm not in PERMS.get(p.role, frozenset()):
            with self._tx():
                self._audit(p, "access.denied", perm, role=p.role)
            raise Forbidden(f"the role {p.role!r} may not do this ({perm})")

    # ------------------------------------------------------------------ authentication
    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("missing bearer token")
        with self._lock:
            row = self.db.execute(
                "SELECT u.username, u.display_name, u.role FROM api_tokens t JOIN users u ON u.username = t.username "
                "WHERE t.token_sha256 = ? AND t.revoked = 0 AND u.active = 1", (hash_token(token),),
            ).fetchone()  # fmt: skip
        if row is None:
            raise Unauthorized("invalid or revoked token")
        return Principal(row["username"], row["display_name"], row["role"])

    def issue_token(self, username: str) -> str:
        """New random token; the plain value is returned once, only its SHA-256 is stored."""
        with self._tx():
            user = self.db.execute("SELECT username FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
            if user is None:
                raise NotFound(f"unknown or inactive user {username!r}")
            token = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO api_tokens (token_sha256, username, created_at) VALUES (?, ?, ?)",
                            (hash_token(token), username, self._now()))  # fmt: skip
            self._audit(SYSTEM, "token.issue", username)
        return token

    # ------------------------------------------------------------------ search index
    def _reindex(self, event_id: int) -> None:
        """Rebuild the index row of one event from the tables (call inside a write transaction)."""
        self.db.execute("DELETE FROM search WHERE rowid = ?", (event_id,))
        e = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if e is None or e["redacted"]:
            return
        r = self.db.execute("SELECT status, transcript FROM recordings WHERE event_id = ?", (event_id,)).fetchone()
        transcript = r["transcript"] if r and r["status"] == "done" and r["transcript"] else ""
        tags = " ".join(json.loads(e["tags"]))
        meta = f"{e['asset_id']} {tags}"
        self.db.execute("INSERT INTO search (rowid, message, transcript, meta) VALUES (?, ?, ?, ?)",
                        (event_id, normalize(e["message"]), normalize(transcript), normalize(meta)))  # fmt: skip

    # ------------------------------------------------------------------ events
    def _event_dict(self, e: sqlite3.Row, r: sqlite3.Row | None, **extra) -> dict:
        out = {
            "id": e["id"], "occurred_at": e["occurred_at"], "asset_id": e["asset_id"], "kind": e["kind"],
            "severity": e["severity"], "message": e["message"], "tags": json.loads(e["tags"]),
            "source": e["source"], "source_ref": e["source_ref"], "redacted": bool(e["redacted"]),
            "created_by": e["created_by"], "created_at": e["created_at"],
            "recording": self._recording_dict(r) if r is not None else None,
        }  # fmt: skip
        out.update(extra)
        return out

    @staticmethod
    def _recording_dict(r: sqlite3.Row) -> dict:
        return {
            "status": r["status"], "attempts": r["attempts"], "language": r["language"],
            "confidence": r["confidence"], "needs_review": bool(r["needs_review"]),
            "corrected": r["corrected_by"] is not None, "version": r["version"],
            "last_error": r["last_error"], "size": r["size"],
        }  # fmt: skip

    def _load(self, event_id: int) -> tuple[sqlite3.Row, sqlite3.Row | None]:
        e = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if e is None:
            raise NotFound(f"no event {event_id}")
        return e, self.db.execute("SELECT * FROM recordings WHERE event_id = ?", (event_id,)).fetchone()

    def ingest(self, p: Principal, req: IngestRequest) -> tuple[dict, bool]:
        """Store an event. ``(source, source_ref)`` makes delivery idempotent: the same content again
        returns the stored event (``created=False``); different content under the same ref is a conflict."""
        self._require(p, "ingest")
        try:
            when = parse_timestamp(req.occurred_at)
        except ValueError as e:
            raise Invalid(f"occurred_at: {e}") from None
        if when > self.clock() + timedelta(seconds=self.future_skew):
            raise Invalid(f"occurred_at is more than {self.future_skew} s in the future (clock skew?)")
        body = {"occurred_at": fmt_timestamp(when), "asset_id": req.asset_id, "kind": req.kind,
                "severity": req.severity, "message": req.message, "tags": sorted(set(req.tags))}  # fmt: skip
        digest = _sha(json.dumps(body, sort_keys=True, ensure_ascii=False))
        with self._tx():
            if req.source_ref is not None:
                old = self.db.execute("SELECT * FROM events WHERE source = ? AND source_ref = ?",
                                      (req.source, req.source_ref)).fetchone()  # fmt: skip
                if old is not None:
                    if old["body_sha256"] != digest:
                        raise SourceRefConflict(
                            f"source_ref {req.source_ref!r} from {req.source!r} was already used for a different event",
                            event_id=old["id"],
                        )
                    return self._event_dict(old, self.db.execute("SELECT * FROM recordings WHERE event_id = ?", (old["id"],)).fetchone()), False  # fmt: skip
            cur = self.db.execute(
                "INSERT INTO events (occurred_at, asset_id, kind, severity, message, tags, source, source_ref, body_sha256,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (body["occurred_at"], req.asset_id, req.kind, req.severity, req.message,
                 json.dumps(body["tags"], ensure_ascii=False), req.source, req.source_ref, digest, p.username, self._now()),
            )  # fmt: skip
            eid = cur.lastrowid
            self._reindex(eid)
            self._audit(p, "event.ingest", f"event:{eid}", kind=req.kind, severity=req.severity,
                        asset_id=req.asset_id, source=req.source, message_sha256=_sha(req.message))  # fmt: skip
            e, r = self._load(eid)
        return self._event_dict(e, r), True

    def get(self, p: Principal, event_id: int) -> dict:
        self._require(p, "read")
        with self._lock:
            e, r = self._load(event_id)
            return self._event_dict(e, r)

    # ------------------------------------------------------------------ search
    def search(
        self,
        p: Principal,
        q: str | None = None,
        *,
        asset_id: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        since: str | None = None,
        until: str | None = None,
        has_recording: bool | None = None,
        scope: str = "all",
        sort: str = "time",
        limit: int = 20,
        cursor: str | None = None,
        offset: int = 0,
    ) -> dict:
        """Filter and full-text search. ``sort="time"`` (newest first) pages with an opaque keyset
        ``cursor``; ``sort="relevance"`` (needs ``q``) pages with ``offset`` (at most MAX_OFFSET)."""
        self._require(p, "read")
        if sort not in ("time", "relevance"):
            raise Invalid("sort must be 'time' or 'relevance'")
        if sort == "relevance" and not q:
            raise Invalid("sort=relevance needs a query")
        if not 1 <= limit <= MAX_LIMIT:
            raise Invalid(f"limit must be between 1 and {MAX_LIMIT}")
        if not 0 <= offset <= MAX_OFFSET:
            raise Invalid(f"offset must be between 0 and {MAX_OFFSET}")
        if cursor is not None and (sort != "time" or offset):
            raise Invalid("cursor is for sort=time and cannot be combined with offset")
        if scope not in COLUMNS:
            raise Invalid(f"'in' must be one of {sorted(COLUMNS)}")
        if kind not in (None, "alarm", "event", "note"):
            raise Invalid("kind must be alarm, event or note")
        if severity not in (None, "info", "warning", "critical"):
            raise Invalid("severity must be info, warning or critical")
        match = build_match(q, scope) if q else None
        where, args = ["1 = 1"], []
        for col, val in (("e.asset_id", asset_id), ("e.kind", kind), ("e.severity", severity)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        for col, op, val in (("e.occurred_at", ">=", since), ("e.occurred_at", "<", until)):
            if val is not None:
                try:
                    where.append(f"{col} {op} ?")
                    args.append(fmt_timestamp(parse_timestamp(val)))
                except ValueError as err:
                    raise Invalid(f"{'since' if op == '>=' else 'until'}: {err}") from None
        if has_recording is not None:
            where.append(f"{'EXISTS' if has_recording else 'NOT EXISTS'} (SELECT 1 FROM recordings x WHERE x.event_id = e.id)")
        if cursor is not None:
            ts, eid = _decode_cursor(cursor)
            where.append("(e.occurred_at, e.id) < (?, ?)")
            args += [ts, eid]
        if match:
            frm = "search JOIN events e ON e.id = search.rowid"
            where.insert(0, "search MATCH ?")
            args.insert(0, match)
            cols = ", snippet(search, -1, '[[', ']]', '…', 12) AS snip, bm25(search, 5.0, 3.0, 1.0) AS rank"
        else:
            frm, cols = "events e", ", NULL AS snip, NULL AS rank"
        order = "rank, e.id DESC" if sort == "relevance" else "e.occurred_at DESC, e.id DESC"
        sql = f"SELECT e.*{cols} FROM {frm} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
        with self._lock:
            rows = self.db.execute(sql, [*args, limit + 1, offset if sort == "relevance" else 0]).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for row in rows:
                r = self.db.execute("SELECT * FROM recordings WHERE event_id = ?", (row["id"],)).fetchone()
                extra = {"snippet": row["snip"]} if match else {}
                if match:
                    extra["score"] = round(-row["rank"], 4)
                items.append(self._event_dict(row, r, **extra))
        out = {"items": items, "count": len(items), "next_cursor": None, "next_offset": None}
        if more and sort == "time":
            out["next_cursor"] = _encode_cursor(rows[-1]["occurred_at"], rows[-1]["id"])
        if more and sort == "relevance":
            out["next_offset"] = offset + limit
        filters = {k: v for k, v in (("asset_id", asset_id), ("kind", kind), ("severity", severity), ("since", since),
                                      ("until", until), ("has_recording", has_recording)) if v is not None}  # fmt: skip
        with self._tx():
            self._audit(p, "search", "events", q=normalize(q) if q else None, scope=scope, sort=sort,
                        filters=filters, returned=len(items))  # fmt: skip
        return out

    # ------------------------------------------------------------------ recordings
    def attach_recording(self, p: Principal, event_id: int, audio: bytes, content_type: str | None) -> tuple[dict, bool]:
        self._require(p, "attach")
        if (content_type or "").split(";")[0].strip().lower() != MEDIA_TYPE:
            raise UnsupportedMedia(f"send Content-Type: {MEDIA_TYPE}")
        if len(audio) > self.max_audio:
            raise TooLarge(f"audio is larger than {self.max_audio} bytes")
        if not audio.startswith(MAGIC):
            raise Invalid("the audio is not a FAKEVOX file")
        digest = hashlib.sha256(audio).hexdigest()
        with self._tx():
            e, r = self._load(event_id)
            if e["redacted"]:
                raise WrongState("the event was redacted; it cannot receive a recording")
            if r is not None:
                if r["sha256"] == digest and r["status"] != "redacted":
                    return self._event_dict(e, r), False
                raise RecordingConflict("this event already has a different recording", event_id=event_id)
            now = self._now()
            self.db.execute(
                "INSERT INTO recordings (event_id, sha256, size, audio, status, next_attempt_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)", (event_id, digest, len(audio), audio, now, now, now))  # fmt: skip
            self._audit(p, "recording.attach", f"event:{event_id}", sha256=digest, size=len(audio))
            e, r = self._load(event_id)
        return self._event_dict(e, r), True

    def get_transcript(self, p: Principal, event_id: int) -> dict:
        self._require(p, "read")
        with self._lock:
            e, r = self._load(event_id)
        if r is None:
            raise NotFound("this event has no recording")
        out = {"event_id": event_id, "status": r["status"], "language": r["language"], "confidence": r["confidence"],
               "needs_review": bool(r["needs_review"]), "corrected": r["corrected_by"] is not None,
               "version": r["version"], "text": r["transcript"] if r["status"] == "done" else None,
               "original_text": r["transcript_original"], "last_error": r["last_error"]}  # fmt: skip
        return out

    def correct_transcript(self, p: Principal, event_id: int, req: CorrectRequest) -> dict:
        """A person fixes the machine transcript. The first machine text is kept as ``original_text``."""
        self._require(p, "correct")
        text = " ".join(req.text.split())
        with self._tx():
            _, r = self._load(event_id)
            if r is None or r["status"] != "done":
                raise WrongState("only a finished transcript can be corrected")
            if r["version"] != req.version:
                raise StaleVersion(f"the transcript is at version {r['version']}, not {req.version}", current_version=r["version"])
            original = r["transcript_original"] if r["transcript_original"] is not None else r["transcript"]
            self.db.execute(
                "UPDATE recordings SET transcript = ?, transcript_sha256 = ?, transcript_original = ?, needs_review = 0,"
                " corrected_by = ?, version = version + 1, updated_at = ? WHERE event_id = ? AND version = ?",
                (text, _sha(text), original, p.username, self._now(), event_id, req.version))  # fmt: skip
            self._reindex(event_id)
            self._audit(p, "transcript.correct", f"event:{event_id}", before_sha256=_sha(r["transcript"] or ""),
                        after_sha256=_sha(text), version=req.version + 1)  # fmt: skip
        return self.get_transcript(p, event_id)

    def retry(self, p: Principal, event_id: int) -> dict:
        self._require(p, "retry")
        with self._tx():
            e, r = self._load(event_id)
            if r is None or r["status"] not in ("failed", "dead"):
                raise WrongState("only a failed recording can be retried")
            now = self._now()
            self.db.execute("UPDATE recordings SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL,"
                            " lease_until = NULL, updated_at = ? WHERE event_id = ?", (now, now, event_id))  # fmt: skip
            self._audit(p, "recording.retry", f"event:{event_id}", previous_status=r["status"])
            e, r = self._load(event_id)
        return self._event_dict(e, r)

    # ------------------------------------------------------------------ redaction
    def redact(self, p: Principal, event_id: int, req: RedactRequest) -> dict:
        """Remove the content of an event (message, audio, transcript, index entry) but keep a tombstone."""
        self._require(p, "redact")
        with self._tx():
            e, r = self._load(event_id)
            if e["redacted"]:
                raise WrongState("the event is already redacted")
            self.db.execute("UPDATE events SET message = ?, tags = '[]', redacted = 1 WHERE id = ?", (REDACTED_TEXT, event_id))
            if r is not None:
                self.db.execute(
                    "UPDATE recordings SET status = 'redacted', audio = NULL, transcript = NULL, transcript_original = NULL,"
                    " lease_until = NULL, last_error = NULL, updated_at = ? WHERE event_id = ?", (self._now(), event_id))  # fmt: skip
            self._reindex(event_id)
            self._audit(p, "event.redact", f"event:{event_id}", reason=req.reason, message_sha256=_sha(e["message"]),
                        had_recording=r is not None)  # fmt: skip
            e, r = self._load(event_id)
        return self._event_dict(e, r)

    # ------------------------------------------------------------------ worker
    def _backoff(self, attempts: int) -> timedelta:
        return timedelta(seconds=min(self.backoff_base * 2 ** (attempts - 1), self.backoff_cap))

    def _claim(self, limit: int) -> list[tuple[int, bytes, int]]:
        with self._tx():
            now = self.clock()
            now_s = fmt_timestamp(now)
            # A worker that died mid-job leaves status 'running' with an expired lease: make it eligible again.
            self.db.execute("UPDATE recordings SET status = 'pending', lease_until = NULL WHERE status = 'running' AND lease_until <= ?", (now_s,))  # fmt: skip
            # Jobs that already used all their attempts (crash loops) stop here instead of running forever.
            for row in self.db.execute("SELECT event_id, attempts FROM recordings WHERE status = 'pending' AND attempts >= ?",
                                       (self.max_attempts,)).fetchall():  # fmt: skip
                self.db.execute("UPDATE recordings SET status = 'dead', last_error = 'gave up: attempts exhausted', updated_at = ?"
                                " WHERE event_id = ?", (now_s, row["event_id"]))  # fmt: skip
                self._audit(SYSTEM, "transcription.dead", f"event:{row['event_id']}", attempts=row["attempts"])
            rows = self.db.execute("SELECT event_id, audio, attempts FROM recordings WHERE status = 'pending' AND next_attempt_at <= ?"
                                   " ORDER BY next_attempt_at, event_id LIMIT ?", (now_s, limit)).fetchall()  # fmt: skip
            lease = fmt_timestamp(now + timedelta(seconds=self.lease_seconds))
            jobs = []
            for row in rows:
                self.db.execute("UPDATE recordings SET status = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ?"
                                " WHERE event_id = ?", (lease, now_s, row["event_id"]))  # fmt: skip
                jobs.append((row["event_id"], row["audio"], row["attempts"] + 1))
            return jobs

    def run_worker(self, p: Principal | None = None, limit: int = 10) -> dict:
        """Process due recordings once. ``p`` is the operator who asked (None for the worker process)."""
        if p is not None:
            self._require(p, "work")
        summary = {"claimed": 0, "done": 0, "retry": 0, "failed": 0, "dead": 0, "stale": 0}
        for event_id, audio, attempt in self._claim(limit):
            summary["claimed"] += 1
            try:
                result, error, permanent = self.transcriber.transcribe(audio), None, False
            except PermanentError as e:
                result, error, permanent = None, str(e), True
            except TransientError as e:
                result, error, permanent = None, str(e), False
            except Exception as e:  # engine bug: treated as transient, details stay in the server log
                log.exception("transcriber crashed on event %s", event_id)
                result, error, permanent = None, f"engine error ({type(e).__name__})", False
            if self._before_finish:
                self._before_finish()
            outcome = self._finish(event_id, attempt, result, error, permanent)
            summary[outcome] += 1
        return summary

    def _finish(self, event_id: int, attempt: int, result, error: str | None, permanent: bool) -> str:
        with self._tx():
            now = self.clock()
            now_s = fmt_timestamp(now)
            guard = ("event_id = ? AND status = 'running' AND attempts = ?", (event_id, attempt))
            if result is not None:
                text = result.text
                conf = result.confidence
                needs = int(conf is not None and (conf < LOW_CONFIDENCE or any(s.conf < LOW_SEGMENT for s in result.segments)))
                cur = self.db.execute(
                    "UPDATE recordings SET status = 'done', lease_until = NULL, last_error = NULL, language = ?, transcript = ?,"
                    f" transcript_sha256 = ?, confidence = ?, needs_review = ?, updated_at = ? WHERE {guard[0]}",
                    (result.language, text, _sha(text), conf, needs, now_s, *guard[1]))  # fmt: skip
                if cur.rowcount != 1:
                    return "stale"
                self._reindex(event_id)
                self._audit(SYSTEM, "transcription.done", f"event:{event_id}", attempt=attempt, language=result.language,
                            confidence=conf, needs_review=bool(needs), transcript_sha256=_sha(text))  # fmt: skip
                return "done"
            if permanent:
                status, outcome = "failed", "failed"
            elif attempt >= self.max_attempts:
                status, outcome = "dead", "dead"
            else:
                status, outcome = "pending", "retry"
            retry_at = fmt_timestamp(now + self._backoff(attempt)) if outcome == "retry" else now_s
            cur = self.db.execute(
                f"UPDATE recordings SET status = ?, lease_until = NULL, last_error = ?, next_attempt_at = ?, updated_at = ? WHERE {guard[0]}",
                (status, error, retry_at, now_s, *guard[1]))  # fmt: skip
            if cur.rowcount != 1:
                return "stale"
            self._audit(SYSTEM, f"transcription.{outcome}", f"event:{event_id}", attempt=attempt, error=error,
                        **({"retry_at": retry_at} if outcome == "retry" else {}))  # fmt: skip
            return outcome

    # ------------------------------------------------------------------ audit and stats
    def audit_entries(self, p: Principal, *, action: str | None = None, actor: str | None = None,
                      after_id: int = 0, limit: int = 50) -> dict:  # fmt: skip
        self._require(p, "audit")
        if not 1 <= limit <= MAX_LIMIT:
            raise Invalid(f"limit must be between 1 and {MAX_LIMIT}")
        where, args = ["id > ?"], [after_id]
        if action:
            where.append("action = ?")
            args.append(action)
        if actor:
            where.append("actor = ?")
            args.append(actor)
        with self._lock:
            rows = self.db.execute(f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY id LIMIT ?", [*args, limit + 1]).fetchall()
        items = [{"id": r["id"], "at": r["at"], "actor": r["actor"], "role": r["role"], "action": r["action"],
                  "target": r["target"], "detail": json.loads(r["detail"]), "hash": r["hash"]} for r in rows[:limit]]  # fmt: skip
        return {"items": items, "next_after_id": items[-1]["id"] if len(rows) > limit else None}

    def audit_verify(self, p: Principal, expected_head: str | None = None) -> dict:
        self._require(p, "audit")
        with self._lock:
            return audit_log.verify(self.db, expected_head)

    def stats(self, p: Principal) -> dict:
        with self._lock:
            group = lambda sql: {r[0]: r[1] for r in self.db.execute(sql)}  # noqa: E731
            return {
                "events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "by_kind": group("SELECT kind, COUNT(*) FROM events GROUP BY kind"),
                "by_severity": group("SELECT severity, COUNT(*) FROM events GROUP BY severity"),
                "recordings": group("SELECT status, COUNT(*) FROM recordings GROUP BY status"),
                "audit_entries": self.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            }


def _encode_cursor(ts: str, event_id: int) -> str:
    return base64.urlsafe_b64encode(f"{ts}|{event_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, eid = raw.partition("|")
        parse_timestamp(ts)
        return ts, int(eid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise Invalid("the cursor is not valid") from None
