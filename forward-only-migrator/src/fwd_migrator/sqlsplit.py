"""Split a migration file into single statements.

``Connection.executescript`` is avoided on purpose: it issues an implicit COMMIT first, which
would break the one-transaction-per-migration guarantee. Instead each statement is run with
``execute`` inside the migrator's own transaction.

``sqlite3.complete_statement`` does the tokenising, so semicolons inside strings, comments and
``CREATE TRIGGER ... BEGIN ...; END;`` bodies are handled by SQLite's own rules.
"""

from __future__ import annotations

import re
import sqlite3

_COMMENTS = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)


def split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            piece = sql[start : i + 1].strip()
            if _COMMENTS.sub("", piece).strip() != ";":  # a bare ";" is an empty statement
                statements.append(piece)
            start = i + 1
    tail = sql[start:].strip()
    if tail and _COMMENTS.sub("", tail).strip():  # keep a final statement that lacks ";"
        statements.append(tail)
    return statements
