import sqlite3

import pytest

from event_archive.errors import Invalid
from event_archive.query import MAX_QUERY_CHARS, MAX_TERMS, build_match


def test_words_are_quoted_and_anded():
    assert build_match("pump leak") == '"pump" "leak"'


def test_phrase_stays_together():
    assert build_match('"bearing temperature" high') == '"bearing temperature" "high"'


def test_prefix_on_a_bare_word_only():
    assert build_match("vibr*") == '"vibr"*'
    assert build_match("vibr* pump") == '"vibr"* "pump"'
    assert build_match('"vibr*"') == '"vibr"'  # inside a phrase it is just punctuation


def test_prefix_needs_two_letters():
    assert build_match("a*") == '"a"'
    assert build_match("ab*") == '"ab"*'


def test_text_is_normalised_like_the_index():
    assert build_match("PMP-۰۰۰۱") == '"pmp" "0001"'
    assert build_match("خنك‌كننده") == '"خنک" "کننده"'


def test_scope_restricts_to_a_column():
    assert build_match("oil", "message") == 'message : ("oil")'
    assert build_match("oil", "transcript") == 'transcript : ("oil")'
    assert build_match("oil", "all") == '"oil"'


def test_unknown_scope_is_rejected():
    with pytest.raises(Invalid):
        build_match("oil", "meta")


@pytest.mark.parametrize("q", ["", "   ", "***", '""', "- - -", '" "'])
def test_nothing_searchable_is_rejected(q):
    with pytest.raises(Invalid, match="no searchable"):
        build_match(q)


def test_length_and_term_limits():
    build_match("a" * MAX_QUERY_CHARS)
    with pytest.raises(Invalid, match="longer"):
        build_match("a" * (MAX_QUERY_CHARS + 1))
    build_match(" ".join(["ab"] * MAX_TERMS))
    with pytest.raises(Invalid, match="more than"):
        build_match(" ".join(["ab"] * (MAX_TERMS + 1)))


HOSTILE = ['" OR 1=1 --', "NEAR(a b)", "message:x", "a AND b", "a OR b", "*", "-a", "^a", "a)", "(a", '"unterminated',
           "a NOT b", "{message}: x", "rowid:1", "x; DROP TABLE events", "'", '""""', "\\", "a*b*", "col:*"]  # fmt: skip


@pytest.mark.parametrize("q", HOSTILE)
def test_hostile_input_never_reaches_fts_as_syntax(q):
    """Whatever the user types, FTS5 must accept the result (no syntax error) and treat it as literals."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE s USING fts5 (message, transcript, meta)")
    db.execute("INSERT INTO s VALUES ('a b x', 'c', 'd')")
    try:
        expr = build_match(q)
    except Invalid:
        return  # nothing left to search for: also fine
    db.execute("SELECT rowid FROM s WHERE s MATCH ?", (expr,)).fetchall()
    assert all(part.startswith('"') for part in expr.split(" ") if part) or '"' in expr
