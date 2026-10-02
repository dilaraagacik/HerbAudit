"""
Resolve a GBIF-vs-AI eventDate mismatch by checking a manual transcription of
the actual sheet, when one is available for that specimen. Used on the
benchmark/audit path, where a GBIF/AI disagreement might be GBIF's fault (a
typo'd date, a placeholder like "1900") rather than the AI's.

Sets herbaudit.audit.dates' module-level `dateutil_parser` directly if unset,
rather than requiring its full _lazy_imports() bundle (opencv/numpy/etc.),
which this module has no other reason to need.

Public entry points:
    load_manual_transcriptions(annotations_path) -> {gbif_id: transcribed_text}
    resolve_date_mismatch(gbif_date, ai_date, manual_text) -> dict | None
"""
import calendar
import re

from herbaudit.audit import dates as _herbaudit_core
from herbaudit.audit.dates import date_accuracy, _parse_date_components
from herbaudit.audit.manual_ground_truth import load_annotation_entries

if _herbaudit_core.dateutil_parser is None:
    from dateutil import parser as _dateutil_parser
    _herbaudit_core.dateutil_parser = _dateutil_parser


def load_manual_transcriptions(annotations_path: str) -> dict[str, str]:
    """{specimen_id: transcribed label text}, from the "transcription" part of each
    entry in the annotations file (see herbaudit/audit/files/annotations.json).

    Returns {} if annotations_path is falsy or the file doesn't exist."""
    entries = load_annotation_entries(annotations_path)
    return {k: v["transcription"] for k, v in entries.items()
            if isinstance(v.get("transcription"), str) and v["transcription"]}

# Month names the extractor recognizes on herbarium labels — European sheets
# commonly write the month as a Roman numeral (e.g. "24.IX.1968") or spell it
# out in German/French/Latin.

# English names come from the stdlib calendar module (locale-independent).
# "sept" is added since calendar.month_abbr only gives the 3-letter "Sep".
_MONTHS: dict[str, int] = {"sept": 9}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i

# Non-English month names seen on herbarium labels — no stdlib/installed
# library covers this, so maintained explicitly.
_MONTHS.update({
    # German
    "januar": 1, "jän": 1, "jaenner": 1, "februar": 2, "märz": 3, "maerz": 3,
    "mär": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10, "okt": 10,
    "dezember": 12, "dez": 12,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "mars": 3,
    "avril": 4, "juin": 6, "juillet": 7, "juil": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12, "déc": 12,
    # Latin (traditional botanical labels)
    "januarius": 1, "februarius": 2, "martius": 3, "aprilis": 4, "maius": 5,
    "junius": 6, "julius": 7, "augustus": 8,
})
# Roman numeral I-XII -> month number (no stdlib roman-numeral parser exists).
_ROMAN_MONTHS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
                  "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_SEP = r"[\s./\-]+"             # ".", "-", "/", or bare space (OCR word-splitting)
_ORDINAL = r"(?:st|nd|rd|th)?"  # optional "24th" / "3rd"
_YEAR = r"(\d{4}|\d{2})"        # full year, or a 2-digit year ("11 Sep. 80")
_CONF_RANK = {"high": 3, "range": 2, "medium": 1, "year": 0}


def _to_year(y_str: str, hint_years: list[int] | None = None) -> int:
    """"80" -> 1980, "03" -> 2003. Prefers whichever century makes the year
    match a hint (GBIF's or the AI's reported year for this specimen);
    otherwise falls back to a sliding pivot on the current year."""
    if len(y_str) == 4:
        return int(y_str)
    yy = int(y_str)
    for hy in (hint_years or []):
        if hy is not None and hy % 100 == yy:
            return hy
    import datetime
    pivot = datetime.datetime.now().year % 100
    return (2000 + yy) if yy <= pivot else (1900 + yy)


def _iter_date_candidates(text: str, hint_years: list[int], hint_full_dates: list[tuple] | None = None):
    """Every date-shaped match anywhere in the text, not just the first (a
    sheet often has several dates on it). Each candidate is
    (repr_string, representative_year, confidence[, range])."""
    iso_re = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
    for m in iso_re.finditer(text):
        y, mo, d = map(int, m.groups())
        yield f"{y:04d}-{mo:02d}-{d:02d}", y, "high"

    roman_re = re.compile(rf"\b(\d{{1,2}}){_ORDINAL}{_SEP}([IVXivx]{{1,4}}){_SEP}{_YEAR}\b")
    for m in roman_re.finditer(text):
        if m.group(2).lower() not in _ROMAN_MONTHS:
            continue
        d = int(m.group(1))
        mo = _ROMAN_MONTHS[m.group(2).lower()]
        y = _to_year(m.group(3), hint_years)
        if 1 <= d <= 31:
            yield f"{y:04d}-{mo:02d}-{d:02d}", y, "high"

    dmy_re = re.compile(rf"\b(\d{{1,2}})[./\-](\d{{1,2}})[./\-]{_YEAR}\b")
    for m in dmy_re.finditer(text):
        d, mo = int(m.group(1)), int(m.group(2))
        y = _to_year(m.group(3), hint_years)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            yield f"{y:04d}-{mo:02d}-{d:02d}", y, "high"

    month_word = "|".join(sorted(_MONTHS, key=len, reverse=True))  # longest-first: "september" before "sep"

    my_d_re = re.compile(rf"\b({month_word})\.?{_SEP}(\d{{1,2}}){_ORDINAL},?{_SEP}{_YEAR}\b", re.IGNORECASE)
    for m in my_d_re.finditer(text):
        mo = _MONTHS[m.group(1).lower()]
        d, y = int(m.group(2)), _to_year(m.group(3), hint_years)
        yield f"{y:04d}-{mo:02d}-{d:02d}", y, "high"

    dm_y_re = re.compile(rf"\b(\d{{1,2}}){_ORDINAL}{_SEP}({month_word})\.?,?{_SEP}{_YEAR}\b", re.IGNORECASE)
    for m in dm_y_re.finditer(text):
        d = int(m.group(1))
        mo = _MONTHS[m.group(2).lower()]
        y = _to_year(m.group(3), hint_years)
        yield f"{y:04d}-{mo:02d}-{d:02d}", y, "high"

    # Roman month + year only, no day — requires punctuation (not bare space)
    # between numeral and year, since short Roman numerals ("I", "V", "X"...)
    # are common English words/abbreviations.
    roman_y_re = re.compile(rf"\b([IVXivx]{{1,4}})[./\-]{_YEAR}\b")
    for m in roman_y_re.finditer(text):
        if m.group(1).lower() not in _ROMAN_MONTHS:
            continue
        mo = _ROMAN_MONTHS[m.group(1).lower()]
        y = _to_year(m.group(2), hint_years)
        yield f"{y:04d}-{mo:02d}", y, "medium"

    m_y_re = re.compile(rf"\b({month_word})\.?,?{_SEP}{_YEAR}\b", re.IGNORECASE)
    for m in m_y_re.finditer(text):
        mo = _MONTHS[m.group(1).lower()]
        y = _to_year(m.group(2), hint_years)
        yield f"{y:04d}-{mo:02d}", y, "medium"

    # Year range ("Annis 1836-38.") — capped at a 20-year span to avoid
    # matching unrelated "YYYY-YY" codes (accession numbers, etc.).
    range_re = re.compile(r"\b(\d{4})\s*[-–]\s*(\d{2}|\d{4})\b")
    for m in range_re.finditer(text):
        start = int(m.group(1))
        end_raw = m.group(2)
        end = int(end_raw) if len(end_raw) == 4 else (start // 100) * 100 + int(end_raw)
        if start <= end <= start + 20:
            yield f"{start}-{end} (range)", None, "range", (start, end)

    # Bare year — too risky on its own (more likely a catalog/collector number),
    # so hint-gated: only surfaced when it equals a year GBIF or the AI reported.
    if hint_years:
        for m in re.finditer(r"\b(\d{4})\b", text):
            y = int(m.group(1))
            if y in hint_years:
                yield f"{y:04d}", y, "year"

    # Bare day/month pair ("6/4") separate from the year — hint-gated: only
    # trusted when it matches (in either order) a full date GBIF/AI reported.
    if hint_full_dates:
        pair_re = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b")
        for m in pair_re.finditer(text):
            a, b = int(m.group(1)), int(m.group(2))
            for hd, hm, hy in hint_full_dates:
                if {a, b} == {hd, hm}:
                    yield f"{hy:04d}-{hm:02d}-{hd:02d}", hy, "high"
                    break


def extract_manual_date(text: str, hint_years: list[int] | None = None, hint_full_dates: list[tuple] | None = None):
    """Best (repr, confidence[, range]) candidate found in the transcription,
    or None. Prefers whichever candidate's year overlaps `hint_years` (GBIF's
    and the AI's own year); otherwise falls back to the highest-confidence,
    first-found candidate."""
    hint_years = [y for y in (hint_years or []) if y is not None]
    candidates = list(_iter_date_candidates(text, hint_years, hint_full_dates))
    if not candidates:
        return None

    def hint_match(c):
        y, conf = c[1], c[2]
        if conf == "range":
            start, end = c[3]
            return any(start <= hy <= end for hy in hint_years)
        return y in hint_years

    candidates.sort(key=lambda c: (hint_match(c), _CONF_RANK.get(c[2], 0)), reverse=True)
    best = candidates[0]
    if best[2] == "range":
        return best[0], "range", best[3]
    return best[0], best[2]


def range_accuracy(rng: tuple, value) -> float:
    """1.0 if `value` names a year inside the sheet's stated [start, end]
    range; 0.0 otherwise. Binary, not partial-credit like date_accuracy()."""
    start, end = rng
    years = [int(y) for y in re.findall(r"\b(\d{4})\b", str(value))]
    if not years:
        _, y, _, _ = _parse_date_components(str(value).split("T")[0].strip())
        if y is not None:
            years = [y]
    if not years:
        return 0.0
    return 1.0 if any(start <= y <= end for y in years) else 0.0


def _verdict_from_scores(vs_ai, vs_gbif):
    """Turn a (manual-vs-AI, manual-vs-GBIF) score pair into (verdict,
    corrected_score). GBIF is the baseline truth throughout this codebase, so
    a correction (non-None corrected_score) is only made when the sheet shows
    GBIF was wrong and the AI's disagreement was actually justified."""
    if vs_ai is not None and (vs_gbif is None or vs_ai > vs_gbif):
        return "sheet supports AI — GBIF likely wrong", vs_ai
    if vs_gbif is not None and (vs_ai is None or vs_gbif > vs_ai):
        return "sheet supports GBIF — AI wrong", None
    if vs_ai is not None and vs_ai >= 1.0:
        return "sheet confirms both AI and GBIF", vs_ai
    return "sheet agrees with neither / inconclusive", None


def resolve_date_mismatch(gbif_date, ai_date, manual_text: str | None) -> dict | None:
    """If GBIF's and the AI's eventDate disagree for a specimen (score <
    1.0), check `manual_text` to decide which one it actually supports.
    Returns None if there's nothing to resolve (GBIF and AI already agree,
    or no manual transcription is available); otherwise a dict:

        {"verdict": str,
         "manual_value": str,             # date/range read off the sheet
         "manual_confidence": str,        # "high"/"medium"/"range"/"year", when not a range
         "manual_vs_ai_score": float,
         "manual_vs_gbif_score": float,
         "corrected_score": float}        # only present when the sheet resolves it

    verdict is one of "sheet supports AI — GBIF likely wrong", "sheet
    supports GBIF — AI wrong", "sheet confirms both AI and GBIF", "sheet
    agrees with neither / inconclusive", or "no date pattern found on sheet
    — review manually" (when manual_text has no recognizable date at all).
    """
    if not manual_text:
        return None
    if date_accuracy(gbif_date, ai_date) >= 1.0:
        return None  # GBIF and AI already agree — nothing to resolve

    parsed = [_parse_date_components(str(v).split("T")[0].strip()) for v in (gbif_date, ai_date)]
    hint_years = [p[1] for p in parsed]
    hint_full_dates = [(p[3], p[2], p[1]) for p in parsed if p[0] == 3]  # (day, month, year)

    cand = extract_manual_date(manual_text, hint_years=hint_years, hint_full_dates=hint_full_dates)
    if cand is None:
        return {"verdict": "no date pattern found on sheet — review manually"}

    if len(cand) == 3:
        manual_value, confidence, rng = cand
        vs_ai, vs_gbif = range_accuracy(rng, ai_date), range_accuracy(rng, gbif_date)
    else:
        manual_value, confidence = cand
        vs_ai, vs_gbif = date_accuracy(manual_value, ai_date), date_accuracy(manual_value, gbif_date)

    verdict, corrected = _verdict_from_scores(vs_ai, vs_gbif)
    out = {"verdict": verdict, "manual_value": manual_value, "manual_confidence": confidence,
           "manual_vs_ai_score": vs_ai, "manual_vs_gbif_score": vs_gbif}
    if corrected is not None:
        out["corrected_score"] = corrected
    return out
