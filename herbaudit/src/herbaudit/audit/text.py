"""Text cleaning and taxon-name utilities."""
from __future__ import annotations

import re

from unidecode import unidecode


def _lazy_imports():
    """Import ftfy on first use, not at CLI startup."""
    global ftfy
    import ftfy

ftfy = None


def clean_text(text):
    """Lowercase, fix encoding/accents, collapse whitespace. Returns 'na' for empty/null."""
    val = str(text).strip().lower()
    if not val or val in {"n/a", "nan", "none", "na", ""}:
        return "na"
    t = unidecode(ftfy.fix_text(val))
    return " ".join(t.split())


# Common non-Latin script blocks (Greek, Cyrillic, Armenian, Hebrew, Arabic,
# Devanagari, Thai, Georgian, Hiragana/Katakana, CJK, Hangul) — deliberately
# excludes the Latin-1 Supplement / Latin Extended ranges, since accented
# Latin letters (é, ñ, ü...) aren't a script difference worth flagging.
_NON_LATIN_SCRIPT_RE = re.compile(
    "[Ͱ-ϿЀ-ӿ԰-֏֐-׿؀-ۿ"
    "ऀ-ॿ฀-๿Ⴀ-ჿ぀-ヿ㐀-䶿"
    "一-鿿가-힣]"
)


def has_non_latin_script(text) -> bool:
    """True if text contains a character from a non-Latin script. Used to
    flag report values worth showing a transliteration alongside — clean_text()
    already unidecodes these before scoring, but that transliterated form
    was never surfaced anywhere a human reviewer could see it."""
    return bool(_NON_LATIN_SCRIPT_RE.search(str(text)))


def transliterate(text) -> str:
    """Latin-alphabet transliteration via unidecode, exposed standalone (not
    through clean_text) so callers can display it without clean_text's
    lowercasing / 'na'-collapsing baked in."""
    return unidecode(str(text))


def strip_authors(name):
    """Return only the binomial (Genus + species), lowercase, stripping any author string."""
    parts = str(name).strip().split()
    binomial = []
    for i, p in enumerate(parts):
        p = p.strip(",;:")  # drop stray punctuation the AI sometimes glues onto a token
        if not p:
            continue
        # A 3rd+ token starts an author citation if capitalized, ends in ".",
        # or is parenthetical ("(L.)"). Checked from i >= 2, not i >= 1,
        # since a capitalized 2nd-token eponym epithet (e.g. "Flotovianus")
        # is indistinguishable from a bare author by this rule alone.
        if i >= 2 and (p[0].isupper() or p.endswith(".") or p.startswith("(")):
            break
        binomial.append(p)
    result = " ".join(binomial[:2]).lower()
    return unidecode(result)


# Rank markers stay lowercase by botanical convention (and so does the
# infraspecific epithet immediately following one) — only the author
# citation gets capitalized.
_RANK_MARKERS = {"subsp.", "ssp.", "var.", "f.", "forma", "subvar.", "cv."}


def _fix_author_case(name: str) -> str:
    """Capitalize a lowercased author abbreviation (e.g. "stev." -> "Stev.")
    before querying GBIF — GBIF's parser otherwise reads a lowercase
    abbreviation as part of the infraspecific epithet and fuzzy-matches to
    an unrelated taxon."""
    tokens = str(name).strip().split()
    fixed = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            fixed.append(tok)
            skip_next = False
            continue
        if tok.lower() in _RANK_MARKERS:
            fixed.append(tok)
            skip_next = True  # next token is the infraspecific epithet, leave it alone
            continue
        if i >= 2 and tok.islower() and tok.endswith("."):
            tok = tok[0].upper() + tok[1:]
        fixed.append(tok)
    return " ".join(fixed)
