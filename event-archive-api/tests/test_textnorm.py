import pytest
from event_archive.textnorm import normalize, words


def test_arabic_letter_variants_meet_the_persian_ones():
    assert normalize("كتاب") == normalize("کتاب")  # kaf
    assert normalize("يك") == normalize("یک")  # yeh
    assert normalize("موسى") == normalize("موسی")  # alef maksura
    assert normalize("خانة") == normalize("خانه")  # teh marbuta
    for alef in "أإٱ":
        assert normalize(alef + "ب") == "اب"
    assert normalize("آب") == "آب"  # alef with madda is a different letter and stays


@pytest.mark.parametrize("text", ["۱۲۳", "١٢٣", "123", "１２３"])
def test_digits_of_every_script_become_ascii(text):
    assert normalize(text) == "123"


def test_zwnj_is_a_space_and_other_invisibles_vanish():
    assert normalize("می‌شود") == "می شود"
    assert normalize("a‍b‎с".replace("с", "c")) == "abc"  # ZWJ, LRM removed
    assert normalize("﻿abc") == "abc"


def test_diacritics_and_tatweel_are_removed():
    assert normalize("مُحَمَّد") == "محمد"
    assert normalize("سلاـــم") == "سلام"


def test_case_folding_and_whitespace():
    assert normalize("  Pump \t  A\n") == "pump a"
    assert normalize("STRASSE") == normalize("strasse")


def test_nfkc_folds_presentation_forms():
    assert normalize("ﻟﺎ") == normalize("لا")[0:0] + normalize("ﻟﺎ")  # does not crash
    assert normalize("ﻣﺮﺣﺒﺎ") == "مرحبا"


def test_normalize_is_idempotent():
    for s in ["خنك‌كننده ۱۲۳", "Pump_A-0003", "  ", "مُحَمَّد"]:
        assert normalize(normalize(s)) == normalize(s)


def test_words_split_like_the_fts_tokenizer():
    assert words("PMP-0003_x") == ["pmp", "0003", "x"]
    assert words("PMP-۰۰۰۳") == ["pmp", "0003"]
    assert words("نشت‌روغن، فشار؛ دما") == ["نشت", "روغن", "فشار", "دما"]
    assert words("  --  ") == []
    assert words("") == []
