"""Reading migration files from a directory.

Rules (all enforced, none merely recommended):

* every ``*.sql`` file is named ``NNNN_snake_case_name.sql``;
* versions start at 1 and have no gaps, so a missing file is noticed instead of skipped;
* the checksum is SHA-256 over the file bytes with ``CRLF`` normalised to ``LF``,
  so a Windows checkout does not look like tampering;
* optional ``-- migrate: <directive>`` lines at the very top; the only directive is
  ``allow-destructive``. Because it is part of the file, it is part of the checksum and
  is visible in review.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import InvalidMigrationFile

NAME_RE = re.compile(r"^(\d{4})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
DIRECTIVE_RE = re.compile(r"^--\s*migrate:\s*(\S+)\s*$")
KNOWN_DIRECTIVES = {"allow-destructive"}


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str
    allow_destructive: bool = False

    @property
    def filename(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


def checksum_of(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _directives(sql: str, filename: str) -> set[str]:
    found: set[str] = set()
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        m = DIRECTIVE_RE.match(stripped)
        if m:
            if m.group(1) not in KNOWN_DIRECTIVES:
                raise InvalidMigrationFile(f"{filename}: unknown directive {m.group(1)!r}")
            found.add(m.group(1))
    return found


def load_migrations(directory: str | Path) -> list[Migration]:
    directory = Path(directory)
    if not directory.is_dir():
        raise InvalidMigrationFile(f"migrations directory not found: {directory}")
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        m = NAME_RE.match(path.name)
        if not m and re.search(r"(^|[._-])(down|undo|rollback)([._-]|$)", path.stem, re.I):
            raise InvalidMigrationFile(
                f"{path.name}: down migrations are not supported (this tool is forward-only). "
                "Fix a mistake with a new migration instead."
            )
        if not m:
            raise InvalidMigrationFile(
                f"{path.name}: file name must look like 0001_add_something.sql "
                "(four digits, lower-case words joined by underscores)"
            )
        version = int(m.group(1))
        if version == 0:
            raise InvalidMigrationFile(f"{path.name}: versions start at 0001")
        if version in found:
            raise InvalidMigrationFile(
                f"version {version:04d} is used by both {found[version].filename} and {path.name}"
            )
        data = path.read_bytes()
        try:
            sql = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InvalidMigrationFile(f"{path.name}: not valid UTF-8 ({e.reason})") from None
        if not sql.strip():
            raise InvalidMigrationFile(f"{path.name}: file is empty")
        found[version] = Migration(
            version=version,
            name=m.group(2),
            sql=sql.replace("\r\n", "\n"),
            checksum=checksum_of(data),
            allow_destructive="allow-destructive" in _directives(sql, path.name),
        )
    for expected, version in enumerate(sorted(found), start=1):
        if version != expected:
            raise InvalidMigrationFile(
                f"migration {expected:04d} is missing (found {version:04d} next); "
                "versions must be contiguous"
            )
    return [found[v] for v in sorted(found)]
