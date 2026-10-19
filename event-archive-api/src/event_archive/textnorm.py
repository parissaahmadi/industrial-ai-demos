"""Text normalisation for indexing and querying Persian (and mixed Persian/English) text.

The same function is applied to what is stored in the search index and to what the user types, so
spelling variants meet in the middle:

* Arabic ``ي ى`` -> Persian ``ی``;  Arabic ``ك`` -> Persian ``ک``;  ``ة`` -> ``ه``;  ``أ إ ٱ`` -> ``ا``
* every decimal digit of any script (``۱۲۳``, ``١٢٣``) -> ASCII ``123``
* diacritics (``Mn``) and tatweel ``ـ`` removed
* ZWNJ (``U+200C``, "half space") -> a normal space, so ``می‌شود`` and ``می شود`` are the same
* other invisible format characters (ZWJ, LRM, RLM, BOM, ...) removed
* NFKC (folds Arabic presentation forms, full-width Latin), case folding, whitespace collapsed
"""

from __future__ import annotations

import re
import unicodedata

_MAP = str.maketrans({
    "ي": "ی",  # ي -> ی
    "ى": "ی",  # ى -> ی
    "ك": "ک",  # ك -> ک
    "ة": "ه",  # ة -> ه
    "أ": "ا",  # أ -> ا
    "إ": "ا",  # إ -> ا
    "ٱ": "ا",  # ٱ -> ا
    "ـ": "",        # tatweel
    "‌": " ",       # ZWNJ
})  # fmt: skip
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[^\W_]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_MAP)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Nd":
            out.append(str(unicodedata.digit(ch)))
        elif cat in ("Mn", "Cf"):
            continue
        else:
            out.append(ch)
    return _SPACES.sub(" ", "".join(out).casefold()).strip()


def words(text: str) -> list[str]:
    """Normalised words, split the way the FTS5 ``unicode61`` tokenizer splits (punctuation and
    ``_`` separate words)."""
    return _WORD.findall(normalize(text))
