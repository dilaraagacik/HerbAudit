"""
Resolve a GBIF-vs-AI field mismatch by checking a manual transcription of the
actual sheet, when one is available — the generalized, all-fields version of
manual_date_reconcile.py's date-specific reconciliation.

Called by get_accuracy() (scoring.py) only after its own field-specific
scoring already found a mismatch (score < 1.0), so this module only ever
overrides an already-detected disagreement.

Date fields (is_date=True) skip the generic word-token matching entirely and
go through manual_date_reconcile's date-pattern extraction instead, since
word-tokenizing and plain CER both handle date notation and off-by-a-digit
years incorrectly.

For every other field, GBIF's and the AI's values are each checked
independently for an exact whole-word match in the transcript (no fuzzy
matching — a near-miss word must not count as confirmed). Whichever side is
confirmed becomes the scoring reference; if both are confirmed, or neither
is, GBIF is the reference (this codebase's GBIF-is-baseline convention).
The score itself is plain CER against that reference.

Public entry point:
    resolve_via_manual_text(gbif_value, ai_value, manual_text, is_date=False)
        -> (score: float, method: str, note: str) | None
    Returns None if manual_text is falsy, so callers can tell "nothing to
    reconcile" apart from a real 0.0 score.
"""
import re

import Levenshtein

from herbaudit.audit.text import clean_text

_STOPWORDS = {"n", "a", "the", "of", "and", "et", "de", "la", "le", "van", "der"}
_COLL_NOISE = {"et", "al", "and", "dr", "coll", "by", "collector", "collected", "leg", "det"}
# herbaudit's own placeholder for an unknown month/day within a partial
# date ("1966-00-00") — never a real, checkable claim, so it shouldn't
# count against a match the way a genuine missing word would.
_ZERO_PLACEHOLDER_RE = re.compile(r"^0+$")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokenize(text) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(str(text))
            if len(t) >= 2 and t.lower() not in _STOPWORDS
            and not _ZERO_PLACEHOLDER_RE.match(t)]


def _exact_found(value, transcription_tokens: set) -> bool:
    """True if every word of value is a whole token somewhere in the
    transcription — not a raw substring check (avoids false positives like
    "Peru" matching inside "Peruviana")."""
    value_tokens = _tokenize(value)
    return bool(value_tokens) and all(t in transcription_tokens for t in value_tokens)


def _token_pair_cer(tru_tokens: list[str], ext_tokens: list[str]) -> float:
    """Same algorithm as scoring.py's token_pair_cer (duplicated here to
    avoid a circular import — scoring.py calls into this module). Pairs
    tokens by best CER match rather than requiring exact equality; pairs
    are claimed globally best-score-first rather than in left-to-right
    order, so a short filler word can't steal another token's real match."""
    pairs = []
    for i, tw in enumerate(tru_tokens):
        for j, ew in enumerate(ext_tokens):
            dist = Levenshtein.distance(tw, ew)
            cer = 1.0 - dist / max(len(tw), len(ew), 1)
            pairs.append((cer, i, j))
    pairs.sort(key=lambda p: -p[0])

    t_used: set[int] = set()
    e_used: set[int] = set()
    total_matched, total_len = 0.0, 0
    for cer, i, j in pairs:
        if i in t_used or j in e_used:
            continue
        t_used.add(i)
        e_used.add(j)
        tw, ew = tru_tokens[i], ext_tokens[j]
        total_matched += 2 * max(len(tw), len(ew)) * cer
        total_len += len(tw) + len(ew)
    for i, tw in enumerate(tru_tokens):
        if i not in t_used:
            total_len += len(tw)
    for j, ew in enumerate(ext_tokens):
        if j not in e_used:
            total_len += len(ew)
    return total_matched / total_len if total_len else 0.0


def _resolve_date_via_manual_text(gbif_value, ai_value, manual_text):
    """Date-aware reconciliation tried before the generic word-token approach
    whenever is_date=True. Parses actual date patterns out of the transcript
    instead of comparing GBIF/AI as plain word tokens.

    Returns (score, method, note), or None if no recognizable date was found,
    or one was found but doesn't clearly back either side."""
    from herbaudit.audit.dates import date_accuracy, _parse_date_components
    from herbaudit.manual_date_reconcile import extract_manual_date, range_accuracy

    parsed = [_parse_date_components(str(v).split("T")[0].strip()) for v in (gbif_value, ai_value)]
    hint_years = [p[1] for p in parsed]
    hint_full_dates = [(p[3], p[2], p[1]) for p in parsed if p[0] == 3]  # (day, month, year)

    cand = extract_manual_date(manual_text, hint_years=hint_years, hint_full_dates=hint_full_dates)
    if cand is None:
        return None

    if len(cand) == 3:
        manual_value, confidence, rng = cand
        vs_ai, vs_gbif = range_accuracy(rng, ai_value), range_accuracy(rng, gbif_value)
    else:
        manual_value, confidence = cand
        vs_ai, vs_gbif = date_accuracy(manual_value, ai_value), date_accuracy(manual_value, gbif_value)

    if vs_ai > vs_gbif:
        return vs_ai, "cer-manual-text", f"date found on sheet: {manual_value!r} ({confidence}) — supports AI"
    if vs_gbif > vs_ai:
        # Sheet backs GBIF — score the AI's value against GBIF's confirmed date
        # so a close-but-wrong AI read still earns partial credit.
        return (date_accuracy(gbif_value, ai_value), "cer-manual-text",
                f"date found on sheet: {manual_value!r} ({confidence}) — supports GBIF")
    if vs_ai >= 1.0:
        return 1.0, "cer-manual-text", f"date found on sheet: {manual_value!r} ({confidence}) — confirms both"
    return None  # a date was found but it's inconclusive — let the generic path have a shot


def resolve_via_manual_text(gbif_value, ai_value, manual_text, is_date=False, is_coll=False, is_geo=False) -> tuple[float, str, str] | None:
    if not manual_text:
        return None

    if is_date:
        date_result = _resolve_date_via_manual_text(gbif_value, ai_value, manual_text)
        if date_result is not None:
            return date_result
        # No usable date pattern in the transcript — fall back to date_accuracy
        # directly rather than the generic word-token/CER stage below, which
        # doesn't understand date semantics (e.g. it'd score "1959-03-24" vs.
        # "1958-03-24" as 90% similar instead of the correct hard 0%).
        from herbaudit.audit.dates import date_accuracy as _date_accuracy_fallback
        return (_date_accuracy_fallback(gbif_value, ai_value), "cer-manual-text",
                "no usable date pattern found on sheet — date_accuracy(GBIF, AI)")

    ls_tokens_set = set(_tokenize(manual_text))

    # Whole-word exact match only — no fuzzy/ratio matching, so a near-miss
    # word (e.g. "Prinshof" vs. transcript's "Pruishof") falls through to the
    # plain CER computation below instead of being rounded up to "confirmed".
    def _confirmed(value):
        return _exact_found(value, ls_tokens_set)

    gbif_ok = _confirmed(gbif_value)
    ai_ok = _confirmed(ai_value)

    if gbif_ok and ai_ok:
        # Both independently found verbatim on the sheet — score full credit
        # rather than diffing the AI's value against GBIF's, which would
        # unfairly punish a shorter-but-correct AI answer (e.g. AI's "Pruishof"
        # vs. GBIF's fuller "Zoutpansberg, Pruishof on plot D18").
        return (1.0, "cer-manual-text",
                "both confirmed (GBIF via exact, AI via exact) — "
                "AI scored on its own transcript match, not diffed against GBIF")
    elif gbif_ok:
        reference, stage = gbif_value, "exact match: GBIF confirmed"
    elif ai_ok:
        reference, stage = ai_value, "exact match: AI confirmed"
    else:
        # Neither side is corroborated by the transcript, so it has nothing to
        # add — return None and let get_accuracy() keep _baseline_accuracy's
        # own score, which already applies field-specific normalization this
        # module's generic CER formula below doesn't know about.
        return None

    t_raw = clean_text(reference)
    e_raw = clean_text(ai_value)

    if is_coll:
        # Same token-pairing treatment as scoring.py's baseline "coll-cer" path
        # (duplicated here to avoid a circular import).
        ref_tokens = [w for w in re.sub(r"[^\w\s]", " ", t_raw).split() if w not in _COLL_NOISE]
        ai_tokens = [w for w in re.sub(r"[^\w\s]", " ", e_raw).split() if w not in _COLL_NOISE]
        ratio = _token_pair_cer(ref_tokens, ai_tokens) if ref_tokens and ai_tokens else 0.0
    elif is_geo:
        # Same word-level token_pair_cer as scoring.py's baseline "geo-cer-bag"
        # path (duplicated here to avoid a circular import).
        def _geo_normalize(text):
            t = re.sub(r"[^\w\s]", " ", text.lower())
            t = re.sub(r"\bn\b", "north", t); t = re.sub(r"\bs\b", "south", t)
            t = re.sub(r"\be\b", "east", t); t = re.sub(r"\bw\b", "west", t)
            return t.split()
        ratio = _token_pair_cer(_geo_normalize(t_raw), _geo_normalize(e_raw))
    else:
        # Everything else (taxonomy, catalog numbers, ...) — same word-level
        # token_pair_cer as the geo/collector branches above.
        ref_tokens = re.sub(r"[^\w\s]", " ", t_raw).split()
        ai_tokens = re.sub(r"[^\w\s]", " ", e_raw).split()
        ratio = _token_pair_cer(ref_tokens, ai_tokens) if ref_tokens and ai_tokens else 0.0

    return ratio, "cer-manual-text", stage
