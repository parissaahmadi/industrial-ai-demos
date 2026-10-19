"""Turn what a person types into a safe FTS5 expression.

User text is never passed to ``MATCH`` as written (FTS5 has its own syntax: ``AND``, ``NEAR``,
``col:``, ``*``, parentheses, ``-``...). Instead it is tokenised here and every word is emitted as
a double-quoted string, which FTS5 treats as a literal. Supported by design, nothing more:

* words are AND-ed:            ``pump leak``
* phrases:                     ``"bearing temperature"``
* prefix on a bare word:       ``vibr*``  (at least 2 letters before the ``*``)
"""

from __future__ import annotations

import re

from .errors import Invalid
from .textnorm import words

MAX_QUERY_CHARS = 200
MAX_TERMS = 12
_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
COLUMNS = {"all": None, "message": "message", "transcript": "transcript"}


def build_match(q: str, scope: str = "all") -> str:
    if scope not in COLUMNS:
        raise Invalid(f"'in' must be one of {sorted(COLUMNS)}")
    if len(q) > MAX_QUERY_CHARS:
        raise Invalid(f"the query is longer than {MAX_QUERY_CHARS} characters")
    parts: list[str] = []
    for phrase, bare in _TOKEN.findall(q):
        if phrase:
            w = words(phrase)
            if w:
                parts.append('"' + " ".join(w) + '"')
        else:
            w = words(bare)
            if not w:
                continue
            prefix = bare.endswith("*") and len(w[-1]) >= 2
            parts.extend(f'"{x}"' for x in w[:-1])
            parts.append(f'"{w[-1]}"' + ("*" if prefix else ""))
    if not parts:
        raise Invalid("the query has no searchable words")
    if len(parts) > MAX_TERMS:
        raise Invalid(f"the query has more than {MAX_TERMS} terms")
    expr = " ".join(parts)
    col = COLUMNS[scope]
    return f"{col} : ({expr})" if col else expr
