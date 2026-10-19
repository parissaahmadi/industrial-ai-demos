"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate9.py        (RUNNER="uv run pytest -q -x" python mutate9.py to choose the test command)
A mutant that makes the suite hang counts as killed (per-mutant timeout, MUTATION_TIMEOUT seconds, default 90).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()
TIMEOUT = int(os.environ.get("MUTATION_TIMEOUT", "90"))
COPY = ["src", "tests", "pyproject.toml"]
S = "src/event_archive/"

# (file, [(old, new), ...], what the bug is)
MUTATIONS = [
    # --- text normalisation
    (S + "textnorm.py", [(r'    "ي": "ی",  # ي -> ی' + "\n", "")], "Arabic yeh is not folded to Persian yeh"),
    (S + "textnorm.py", [(r'    "ك": "ک",  # ك -> ک' + "\n", "")], "Arabic kaf is not folded to Persian kaf"),
    (S + "textnorm.py", [(r'"‌": " ",', r'"‌": "",')], "ZWNJ is deleted instead of becoming a space"),
    (S + "textnorm.py", [('if cat == "Nd":', "if False:")], "digits of other scripts are not converted"),
    (S + "textnorm.py", [('elif cat in ("Mn", "Cf"):', 'elif cat in ("Cf",):')], "diacritics are kept"),
    (S + "textnorm.py", [('"".join(out).casefold()', '"".join(out)')], "no case folding"),
    (S + "textnorm.py", [('unicodedata.normalize("NFKC", text)', 'unicodedata.normalize("NFC", text)')], "presentation forms are not folded"),
    (S + "textnorm.py", [(r'_WORD = re.compile(r"[^\W_]+")', r'_WORD = re.compile(r"\w+")')], "underscore treated as part of a word"),
    # --- query builder
    (S + "query.py", [("""parts.extend(f'"{x}"' for x in w[:-1])""", "parts.extend(w[:-1])")], "words are not quoted (FTS syntax injection)"),
    (S + "query.py", [("len(w[-1]) >= 2", "len(w[-1]) >= 1")], "one-letter prefixes allowed"),
    (S + "query.py", [('prefix = bare.endswith("*") and', "prefix = False and")], "prefix search never works"),
    (S + "query.py", [("if len(parts) > MAX_TERMS:", "if len(parts) >= MAX_TERMS:")], "term limit off by one"),
    (S + "query.py", [("if len(q) > MAX_QUERY_CHARS:", "if len(q) >= MAX_QUERY_CHARS:")], "length limit off by one"),
    (S + "query.py", [('return f"{col} : ({expr})" if col else expr', "return expr")], "scope ignored"),
    # --- audit chain
    (S + "audit.py", [('        if r["prev_hash"] != prev:\n            return _bad(count, r["id"], "prev_hash does not match the previous row")\n', "")], "prev_hash column not checked"),
    (S + "audit.py", [('        if r["id"] != expected_id:', "        if False:")], "missing / inserted rows not detected"),
    (S + "audit.py", [('FIELDS = ("id", "at", "actor", "role", "action", "target", "detail")', 'FIELDS = ("id", "at", "actor", "role", "action", "detail")')], "target not covered by the hash"),
    (S + "audit.py", [('new_id, prev = (last["id"] + 1, last["hash"]) if last else (1, GENESIS)', 'new_id, prev = (last["id"] + 1, GENESIS) if last else (1, GENESIS)')], "rows are not chained"),
    (S + "audit.py", [("if expected_head is not None and expected_head != prev:", "if False:")], "expected head ignored"),
    (S + "audit.py", [("json.dumps(detail, sort_keys=True,", "json.dumps(detail, sort_keys=False,")], "detail not canonical"),
    (S + "audit.py", [("if r[\"hash\"] != row_hash(prev, row):", "if False:")], "row content not checked against its hash"),
    # --- database
    (S + "db.py", [("BEFORE UPDATE ON audit\nBEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;", "BEFORE UPDATE ON audit\nBEGIN SELECT 1; END;")], "audit rows can be updated"),
    (S + "db.py", [("BEFORE DELETE ON audit\nBEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;", "BEFORE DELETE ON audit\nBEGIN SELECT 1; END;")], "audit rows can be deleted"),
    (S + "db.py", [('return hashlib.sha256(token.encode("utf-8")).hexdigest()', "return token")], "tokens stored in clear text"),
    (S + "db.py", [("CREATE UNIQUE INDEX idx_events_source_ref", "CREATE INDEX idx_events_source_ref")], "database does not enforce source_ref uniqueness"),
    # --- permissions
    (S + "service.py", [("if perm not in PERMS.get(p.role, frozenset()):", "if False:")], "permission check removed"),
    (S + "service.py", [('"operator": frozenset({"read", "ingest", "attach", "correct", "retry", "work"})', '"operator": frozenset({"read", "ingest", "attach", "correct", "retry", "work", "redact"})')], "operators may redact"),
    (S + "service.py", [('"auditor": frozenset({"audit"})', '"auditor": frozenset({"audit", "read"})')], "auditors may read event content"),
    (S + "service.py", [('"viewer": frozenset({"read"})', '"viewer": frozenset({"read", "ingest"})')], "viewers may ingest"),
    (S + "service.py", [('"admin": frozenset({"read", "ingest", "attach", "correct", "retry", "work", "redact", "audit"})', '"admin": frozenset({"read", "ingest", "attach", "correct", "retry", "work", "redact"})')], "admins cannot read the audit trail"),
    (S + "service.py", [("AND t.revoked = 0 AND u.active = 1", "AND u.active = 1")], "revoked tokens still work"),
    (S + "service.py", [(' AND u.active = 1", (hash_token(token),)', '", (hash_token(token),)')], "inactive users can still log in"),
    # --- ingest
    (S + "service.py", [("if when > self.clock() + timedelta(seconds=self.future_skew):", "if when >= self.clock() + timedelta(seconds=self.future_skew):")], "future-skew boundary off by one"),
    (S + "models.py", [("if dt.tzinfo is None:", "if False:")], "naive timestamps accepted"),
    (S + "service.py", [('if old["body_sha256"] != digest:', "if False:")], "same source_ref with other content treated as a replay"),
    (S + "service.py", [('"tags": sorted(set(req.tags))', '"tags": req.tags')], "tag order changes the replay fingerprint"),
    (S + "service.py", [("message_sha256=_sha(req.message))", "message=req.message)")], "audit trail stores the message text"),
    (S + "service.py", [('if e is None or e["redacted"]:', 'if e is None:')], "redacted events stay in the index"),
    # --- search
    (S + "service.py", [('where.append("(e.occurred_at, e.id) < (?, ?)")', 'where.append("(e.occurred_at, e.id) <= (?, ?)")')], "cursor repeats the last row"),
    (S + "service.py", [('"e.occurred_at DESC, e.id DESC"', '"e.occurred_at DESC"')], "no tie-break in time order"),
    (S + "service.py", [("more = len(rows) > limit", "more = len(rows) >= limit")], "a next page is promised when there is none"),
    (S + "service.py", [('(("e.occurred_at", ">=", since), ("e.occurred_at", "<", until))', '(("e.occurred_at", ">", since), ("e.occurred_at", "<", until))')], "since is exclusive"),
    (S + "service.py", [('("e.occurred_at", "<", until))', '("e.occurred_at", "<=", until))')], "until is inclusive"),
    (S + "service.py", [("if not 1 <= limit <= MAX_LIMIT:\n            raise Invalid(f\"limit must", "if not 1 <= limit < MAX_LIMIT:\n            raise Invalid(f\"limit must")], "limit boundary"),
    (S + "service.py", [("if not 0 <= offset <= MAX_OFFSET:", "if not 0 <= offset < MAX_OFFSET:")], "offset boundary"),
    (S + "service.py", [('"rank, e.id DESC"', '"rank DESC, e.id DESC"')], "worst match first"),
    (S + "service.py", [("q=normalize(q) if q else None", "q=q")], "audit stores the raw query"),
    (S + "service.py", [('"search", "events", q=', '"search.run", "events", q=')], "searches not audited under their name"),
    (S + "service.py", [('        if scope not in COLUMNS:\n            raise Invalid(f"\'in\' must be one of {sorted(COLUMNS)}")\n', "")], "scope validated only when a query is given"),
    (S + "service.py", [("if has_recording is not None:", "if False:")], "has_recording filter ignored"),
    # --- recordings
    (S + "service.py", [('if r["sha256"] == digest and r["status"] != "redacted":', "if False:")], "identical re-upload is a conflict"),
    (S + "service.py", [('.strip().lower() != MEDIA_TYPE:', '.strip().lower() == "":')], "media type not checked"),
    (S + "service.py", [("if len(audio) > self.max_audio:", "if len(audio) >= self.max_audio:")], "audio size limit off by one"),
    (S + "service.py", [("if not audio.startswith(MAGIC):", "if False:")], "any bytes accepted as audio"),
    (S + "service.py", [('            if e["redacted"]:\n                raise WrongState("the event was redacted; it cannot receive a recording")', '            if False:\n                raise WrongState("the event was redacted; it cannot receive a recording")')], "recordings attach to redacted events"),
    # --- corrections, retry, redaction
    (S + "service.py", [('if r["version"] != req.version:', "if False:")], "stale corrections overwrite newer ones"),
    (S + "service.py", [('original = r["transcript_original"] if r["transcript_original"] is not None else r["transcript"]', 'original = r["transcript"]')], "second correction overwrites the machine original"),
    (S + "service.py", [('transcript_original = ?, needs_review = 0,"', 'transcript_original = ?,"')], "correction leaves the review flag set"),
    (S + "service.py", [('if r is None or r["status"] != "done":', "if r is None:")], "unfinished transcripts can be corrected"),
    (S + "service.py", [('r["status"] not in ("failed", "dead")', 'r["status"] not in ("failed",)')], "dead recordings cannot be retried"),
    (S + "service.py", [("status = 'redacted', audio = NULL,", "status = 'redacted',")], "audio survives redaction"),
    (S + "service.py", [("UPDATE events SET message = ?, tags = '[]', redacted = 1", "UPDATE events SET message = ?, redacted = 1")], "tags survive redaction"),
    (S + "service.py", [("transcript = NULL, transcript_original = NULL,", "transcript = NULL,")], "machine original survives redaction"),
    (S + "service.py", [('if e["redacted"]:\n                raise WrongState("the event is already redacted")', 'if False:\n                raise WrongState("the event is already redacted")')], "double redaction accepted"),
    # --- worker
    (S + "service.py", [("2 ** (attempts - 1)", "2 ** attempts")], "back-off starts too long"),
    (S + "service.py", [("min(self.backoff_base * 2 ** (attempts - 1), self.backoff_cap)", "self.backoff_base * 2 ** (attempts - 1)")], "back-off not capped"),
    (S + "service.py", [("status = 'running' AND lease_until <= ?", "status = 'running' AND lease_until < ?")], "lease boundary off by one"),
    (S + "service.py", [("status = 'running' AND lease_until <= ?", "status = 'running' AND lease_until <= '9999'")], "live leases are stolen"),
    (S + "service.py", [("WHERE status = 'pending' AND attempts >= ?", "WHERE status = 'pending' AND attempts > ?")], "crash loops are not stopped"),
    (S + "service.py", [("status = 'pending' AND next_attempt_at <= ?", "status = 'pending' AND next_attempt_at < ?")], "due time boundary off by one"),
    (S + "service.py", [("status = 'pending' AND next_attempt_at <= ?", "status = 'pending'")], "back-off ignored"),
    (S + "service.py", [("guard = (\"event_id = ? AND status = 'running' AND attempts = ?\", (event_id, attempt))", "guard = (\"event_id = ? AND status = 'running'\", (event_id,))")], "late result of a replaced worker is accepted"),
    (S + "service.py", [("guard = (\"event_id = ? AND status = 'running' AND attempts = ?\", (event_id, attempt))", "guard = (\"event_id = ?\", (event_id,))")], "result accepted for any state (redaction can be undone)"),
    (S + "service.py", [("conf < LOW_CONFIDENCE", "conf <= LOW_CONFIDENCE")], "review threshold (average) off by one"),
    (S + "service.py", [("s.conf < LOW_SEGMENT", "s.conf <= LOW_SEGMENT")], "review threshold (segment) off by one"),
    (S + "service.py", [("elif attempt >= self.max_attempts:", "elif attempt > self.max_attempts:")], "one attempt too many"),
    (S + "service.py", [("            if permanent:\n                status, outcome", "            if False:\n                status, outcome")], "permanent failures are retried"),
    (S + "service.py", [('f"engine error ({type(e).__name__})"', "str(e)")], "engine error text leaks to clients"),
    (S + "service.py", [("self._reindex(event_id)\n                self._audit(SYSTEM, \"transcription.done\"", "self._audit(SYSTEM, \"transcription.done\"")], "finished transcripts are not indexed"),
    (S + "service.py", [("            self._reindex(event_id)\n            self._audit(p, \"transcript.correct\"", "            self._audit(p, \"transcript.correct\"")], "corrected transcripts are not re-indexed"),
    (S + "service.py", [('base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()\n        ts, _, eid = raw.partition("|")\n        parse_timestamp(ts)', 'base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()\n        ts, _, eid = raw.partition("|")')], "cursor timestamp not validated"),
    # --- transcribe
    (S + "transcribe.py", [("any(not 0.0 <= s.conf <= 1.0 or s.end < s.start for s in segs)", "any(s.end < s.start for s in segs)")], "confidence range not validated"),
    (S + "transcribe.py", [("if n <= int(doc.get(\"fail_attempts\", 0)):", "if n < int(doc.get(\"fail_attempts\", 0)):")], "mock fails one time too few"),
    # --- HTTP layer
    (S + "api.py", [("        if total > limit:", "        if False:")], "streamed (chunked) bodies are not limited"),
    (S + "api.py", [("int(declared) > limit", "False")], "declared length not checked up front"),
    (S + "api.py", [('"cache-control": "no-store"', '"cache-control": "public"')], "responses cacheable"),
    (S + "api.py", [("""headers = {"www-authenticate": 'Bearer realm="event-archive"'} if isinstance(exc, Unauthorized) else None""", "headers = None")], "401 without WWW-Authenticate"),
    (S + "api.py", [('"something went wrong on the server")', "str(exc))")], "internal errors leak details"),
    (S + "api.py", [("audio = await _read_body(request, a.max_audio)", "audio = await _read_body(request, MAX_JSON)")], "audio limited to the JSON limit"),
    (S + "api.py", [('scope=qp.get("in", "all")', 'scope="all"')], "`in` parameter ignored"),
    (S + "api.py", [('return ok(event, 201 if created else 200, None if created else {"idempotent-replayed": "true"})', "return ok(event, 201, None)")], "re-upload answers 201"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return 124


def main() -> int:
    survived = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (rel, edits, why) in enumerate(MUTATIONS, 1):
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
                    print(f"{i:2} SETUP ERROR  pattern not found in {rel}: {old[:70]!r}")
                    return 2
                text = text.replace(old, new, 1)
            target.write_text(text)
            killed = run(tree) != 0
            print(f"{i:2} {'killed  ' if killed else 'SURVIVED'} {Path(rel).name:14} {why}", flush=True)
            if not killed:
                survived.append(why)
            shutil.rmtree(tree, ignore_errors=True)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
