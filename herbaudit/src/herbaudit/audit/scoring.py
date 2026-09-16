"""Accuracy scoring engine (get_accuracy) — field-by-field comparison logic
used by report.build_card."""
from __future__ import annotations

import re

import Levenshtein

from .text import clean_text, strip_authors, _RANK_MARKERS
from .dates import date_accuracy
from .gbif import check_label_synonym, _gbif_match, _gbif_match_with_authorship_retry
from .wfo import check_synonym_fallback, _nearest_taxon


def _lazy_imports():
    """Import hdx.location.country.Country on first use, not at CLI startup."""
    global Country
    from hdx.location.country import Country

Country = None


EXCLUDED_SENTINEL = "__SYNONYM_EXCLUDED__"


def token_pair_cer(tru_tokens: list[str], ext_tokens: list[str]) -> float:
    """Score token-level similarity by pairing tru_tokens/ext_tokens globally
    best-match-first (not left-to-right), so a short common word can't steal
    the pairing slot meant for the real match. Combines matched/total
    character counts into one CER-based ratio; any unpaired token still
    counts its full length against the total."""
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


def get_accuracy(target, extraction, is_tax=False, is_coll=False,
                 is_date=False, is_geo=False, gbif_meta=None, manual_text=None,
                 is_epithet=False):
    e_raw = clean_text(extraction)
    t_raw = clean_text(target)

    if t_raw == "na":
        # GBIF truth itself isn't available — excluded from the average
        # (method == "N/A" is what build_card checks to skip this field).
        return 0.0, "N/A", "#f8f9fa", ""

    if e_raw == "na":
        if is_tax or is_coll or is_date:
            # Truth IS available but the AI didn't extract it — score as a
            # real miss (not N/A) so it counts against the average. Scoped to
            # taxonomy/collector/date since those are almost always printed
            # on the label; geography is excluded because some GBIF geo
            # values (e.g. stateProvince) are curator-added via geocoding,
            # not label-read.
            return 0.0, "missing", "#f8d7da", "AI did not extract this field"
        return 0.0, "N/A", "#f8f9fa", ""

    score, method, color, note = _baseline_accuracy(
        t_raw, e_raw, target, extraction, is_tax, is_coll, is_date, is_geo, gbif_meta,
        is_epithet=is_epithet)

    # A mismatch might be GBIF's fault, not the AI's — when a manual sheet
    # transcription is available and the score above found a real mismatch
    # (not a None score, which means synonym/off-target exclusion rather
    # than "wrong"), override it via manual_text_reconcile's exact/fuzzy/CER
    # resolution against whichever side (GBIF or the sheet) it confirms.
    if score is not None and score < 1.0 and manual_text:
        from herbaudit.manual_text_reconcile import resolve_via_manual_text
        resolved = resolve_via_manual_text(target, extraction, manual_text, is_date=is_date, is_coll=is_coll, is_geo=is_geo)
        if resolved is not None:
            score, method, note = resolved
            color = "#d1e7dd" if score >= 0.85 else "#f8d7da"

    return score, method, color, note


def _baseline_accuracy(t_raw, e_raw, target, extraction, is_tax, is_coll, is_date, is_geo, gbif_meta,
                        is_epithet=False):
    """Field-specific baseline scoring, consulted before any manual-text override."""
    if is_geo:
        def _geo_score():
            def normalize_hdx(name, original=None):
                manual_aliases = {
                    "chinese taipei": "taiwan",
                    "peoples republic of china": "china",
                    "republic of china": "taiwan"
                }
                n = name.lower().strip()
                n = manual_aliases.get(n, n)
                # Strip punctuation first — HDX's matcher chokes on
                # abbreviations like "U.S.A." but resolves "usa" fine.
                n = re.sub(r"[^\w\s]", "", n)
                try:
                    iso3 = Country.get_iso3_country_code(n)
                    if iso3:
                        return iso3
                except Exception:
                    pass
                # Fall back to the original (pre-unidecode) text — HDX
                # recognizes some native-script exonyms directly but not
                # their unidecode-transliterated form.
                if original is not None and str(original) != name:
                    orig_stripped = re.sub(r"[^\w\s]", "", str(original).strip())
                    try:
                        iso3 = Country.get_iso3_country_code(orig_stripped)
                        if iso3:
                            return iso3
                    except Exception:
                        pass
                return n

            t_norm = normalize_hdx(t_raw, target)
            e_norm = normalize_hdx(e_raw, extraction)

            if t_norm == e_norm:
                return 1.0, "geo-hdx-match", "#d1e7dd", f"Standardized: {t_norm}"

            # Exact match once whitespace is stripped too (e.g. "Viet Nam" vs
            # "Vietnam") — plain equality, not substring containment, so it
            # can't false-match prefix pairs like "Niger"/"Nigeria".
            def _nospace(s):
                return re.sub(r"\s+", "", re.sub(r"[^\w\s]", "", s.lower()))
            if _nospace(t_raw) == _nospace(e_raw):
                return 1.0, "geo-nospace-match", "#d1e7dd", "Exact match ignoring spacing"

            def normalize_for_similarity(text):
                t = text.lower()
                t = re.sub(r"[^\w\s]", " ", t) # Strip punctuation
                t = re.sub(r"\bn\b", "north", t); t = re.sub(r"\bs\b", "south", t)
                t = re.sub(r"\be\b", "east", t); t = re.sub(r"\bw\b", "west", t)
                return " ".join(t.split())

            # Word-level token_pair_cer match — tokenized into individual
            # words rather than comma-split clauses, since two answers can
            # punctuate the same content differently (comma vs. dash).
            t_words = normalize_for_similarity(t_raw).split()
            e_words = normalize_for_similarity(e_raw).split()
            ratio = token_pair_cer(t_words, e_words)

            return ratio, "geo-cer-bag", ("#d1e7dd" if ratio >= 0.70 else "#f8d7da"), ""

        return _geo_score()

    if is_tax:
        clean_target = strip_authors(target)
        clean_extr   = strip_authors(extraction)
        if t_raw == e_raw:
            return 1.0, "Taxon-Match", "#d1e7dd", "Exact match"

        # Below this, target/extraction are queried against GBIF/WFO's
        # backbone as given, which assumes independent taxonomic standing —
        # not valid for a bare specific epithet (the same Latin word gets
        # reused across unrelated genera/families). Skipped for
        # is_epithet=True, which falls straight through to plain CER
        # comparison below.
        #
        # Also skipped when the stripped binomial already matches exactly:
        # at that point there's no species-identity question left for
        # GBIF/WFO to resolve, so any remaining difference (t_raw != e_raw,
        # or we wouldn't be here) is purely authorship/formatting — falling
        # into check_synonym_fallback here would misreport it as a "synonym"
        # relationship (WFO/GBIF backbone can't tell "same name, different
        # authorship" apart from "different name, same accepted taxon") and
        # wrongly exclude it from scoring instead of letting the CER
        # fallback below reflect the actual authorship discrepancy.
        if is_epithet or clean_target == clean_extr:
            pass
        else:
            is_syn2, syn2_note = check_synonym_fallback(target, extraction)
            if is_syn2:
                return None, EXCLUDED_SENTINEL, "#fff8e1", syn2_note
            is_label_syn, found_in = check_label_synonym(extraction, gbif_meta)
            if is_label_syn:
                return None, EXCLUDED_SENTINEL, "#fff8e1", found_in

            # Neither a synonym nor a verbatim-label match — but if GBIF's own
            # backbone recognizes the AI's name AS WRITTEN (an EXACT match)
            # as a real taxon at all, it's not OCR garble, just a
            # different/outdated determination — exclude it from CER
            # scoring, flagged distinctly from an actual synonym. A FUZZY
            # match doesn't qualify: GBIF had to rewrite the string to find
            # a hit, so the literal extracted text isn't itself a real name.
            a_match = _gbif_match_with_authorship_retry(extraction)
            if a_match is not None and a_match.get("matchType") == "EXACT":
                status = (a_match.get("status") or "recognized").lower()
                article = "an" if status[:1] in "aeiou" else "a"
                return None, EXCLUDED_SENTINEL, "#fff8e1", (
                    f"NOREL||'{clean_extr}' is {article} {status} name in GBIF's backbone taxonomy, "
                    f"but has no relationship to GBIF truth name '{clean_target}'"
                )

    if is_date:
        d = date_accuracy(target, extraction)
        return d, "date-eval", ("#d1e7dd" if d >= 1.0 else "#f8d7da"), ""

    if is_coll:
        noise = {"et", "al", "and", "dr", "coll", "by", "collector", "collected", "leg", "det"}

        def _coll_tokens(text):
            # Strip punctuation; single-letter tokens (initials) are kept and
            # scored directly by token_pair_cer below.
            stripped = re.sub(r"[^\w\s]", " ", text)
            return [w for w in stripped.split() if w not in noise]

        ext_tokens = _coll_tokens(e_raw)
        tru_tokens = _coll_tokens(t_raw)

        if ext_tokens and tru_tokens:
            # Pair each truth token with its best-matching extraction token
            # via per-token CER, so initials help confirm a match ("E.P.
            # Amangst" vs "Unangst, E. P.") without being able to fake one
            # alone — a matching initial can't rescue an unrelated surname
            # ("J. Smith" vs "J. Anderson" still scores low).
            ratio = token_pair_cer(tru_tokens, ext_tokens)
            return ratio, "coll-cer", ("#d1e7dd" if ratio >= 0.85 else "#f8d7da"), ""

    # Denominator is max(len(t), len(e)) so correct extra detail beyond a
    # short truth value isn't penalized as if it were wrong.
    cer_t, cer_e = t_raw, e_raw
    if is_tax:
        # Author-citation punctuation/spacing is inconsistent across sources
        # ("G.Don" vs "G. Don", "(Bolton)Gray" vs "(Bolton) Gray") without
        # being a real transcription error — strip it before diffing so
        # only actual letter differences (a genuinely different name/author)
        # cost points, not formatting noise.
        def _has_rank_marker(s):
            return any(tok in _RANK_MARKERS for tok in s.split())

        forgiven = False
        if _has_rank_marker(e_raw) and not _has_rank_marker(t_raw):
            # AI named an infraspecific rank (subsp./var./f., ...) the truth
            # doesn't have. Verify it's a real GBIF-recognized taxon before
            # forgiving it — GBIF reports matchType "HIGHERRANK" (falling
            # back to the bare species) when it can't confirm the specific
            # infraspecific epithet, which catches a fabricated one instead
            # of giving any rank-marker-shaped text a free pass.
            infra = _gbif_match(extraction)
            forgiven = (
                infra is not None and infra.get("matchType") == "EXACT"
                and infra.get("rank") not in (None, "SPECIES", "GENUS", "FAMILY", "ORDER")
            )
        if forgiven:
            cer_t = cer_e = ""
        else:
            cer_t = re.sub(r"[^\w]", "", t_raw)
            cer_e = re.sub(r"[^\w]", "", e_raw)
    cer = max(0.0, 1.0 - Levenshtein.distance(cer_t, cer_e) / max(len(cer_t), len(cer_e), 1))
    note = ""
    if is_tax:
        # Name-similarity lookup only — doesn't verify `nearest` is a
        # synonym of the truth (the synonym checks above handle that).
        # Reached when there's genuinely no synonym relationship, or
        # WFO/GBIF backbone couldn't resolve it; either way this can't tell
        # those apart, so it reports only the closest known name.
        src, nearest, sim = _nearest_taxon(extraction)
        if nearest is None:
            note = "Not found in WFO — possible OCR/extraction error"
        elif sim >= 0.98:
            note = f"Closest {src} match: <i>{nearest}</i> ({int(sim * 100)}% similar) — relationship to GBIF truth not verified"
        elif sim >= 0.60:
            note = f"Closest {src} match: <i>{nearest}</i> ({int(sim * 100)}% similar)"
        else:
            note = "No close match in WFO — possible OCR/extraction error"
    return cer, "cer", ("#d1e7dd" if cer >= 0.85 else "#f8d7da"), note
