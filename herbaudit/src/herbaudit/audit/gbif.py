"""GBIF data fetching, field extraction, and GBIF-backbone/fuzzy name matching."""
from __future__ import annotations

import time

import requests

from .text import strip_authors, _fix_author_case


def safe_json(r):
    try:
        return r.json() if r.ok and r.text.strip() else {}
    except:
        return {}


def _gbif_get(url, params=None, headers=None, timeout=15, retries=4):
    """GET with retry-with-backoff for GBIF's occasional timeouts and
    503/429 responses under load — without it, a single transient hiccup on
    any per-specimen request silently dropped that specimen as "no GBIF
    data found" even though the record genuinely exists. Honors GBIF's own
    Retry-After header when sent, same backoff as the network-error case
    otherwise."""
    last_exc = None
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            resp = None
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
            continue

        if resp.status_code in (503, 429) and attempt < retries:
            retry_after = resp.headers.get("Retry-After")
            delay = (float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit()
                     else 0.5 * (attempt + 1))
            time.sleep(delay)
            continue

        return resp

    if resp is not None:
        return resp  # exhausted retries but still got a response (e.g. persistent 503) — let the caller see it
    raise last_exc


def fetch_gbif_data(gid):
    """Fetch occurrence, fragment, and verbatim records from GBIF by ID or occurrenceID."""
    if not gid or gid in ("nan", "", "None"):
        return {}, {}, {}

    try:
        gid = str(gid).strip()

        if gid.isdigit():
            num_id = gid
        else:
            r = _gbif_get(
                "https://api.gbif.org/v1/occurrence/search",
                params={"occurrenceID": gid},
            ).json()
            results = r.get("results", [])
            if not results:
                return {}, {}, {}
            num_id = results[0].get("gbifID")
            if not num_id:
                return {}, {}, {}

        occ_resp = _gbif_get(f"https://api.gbif.org/v1/occurrence/{num_id}")
        if occ_resp.status_code in (503, 429):
            # Still failing after _gbif_get's own retries — GBIF itself is
            # unavailable/rate-limiting right now, not "this specimen has no
            # occurrence". Raise distinctly so the caller doesn't conflate it
            # with a genuinely-unpublished specimen.
            raise RuntimeError(
                f"GBIF temporarily unavailable (HTTP {occ_resp.status_code}) after retries — "
                f"try re-running; the occurrence itself is very likely fine"
            )
        occ  = occ_resp.json()
        frag = _gbif_get(f"https://api.gbif.org/v1/occurrence/{num_id}/fragment")
        verb = _gbif_get(f"https://api.gbif.org/v1/occurrence/{num_id}/verbatim")

        return occ, safe_json(frag), safe_json(verb)

    except Exception as e:
        print(f"GBIF fetch error for {gid}: {e}")
        return {}, {}, {}


def smart_get(field, frag, verb, occ):
    """
    Pull a field value from GBIF sources in priority order:
    fragment → verbatim → occurrence.
    Taxonomy fields (genus, specificEpithet, scientificName) use deep search.
    """
    def valid(val):
        return val and str(val).strip().lower() not in {"nan", "none", "", "n/a"}

    def deep_get(data, target):
        if isinstance(data, dict):
            for k, v in data.items():
                if k == target and valid(v):
                    return v
                result = deep_get(v, target)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = deep_get(item, target)
                if result:
                    return result
        return None

    if field in {"genus", "specificEpithet", "scientificName"}:
        for source in (frag, verb):
            if source:
                val = deep_get(source, field)
                if valid(val):
                    return val
        if occ:
            val = occ.get(field)
            if valid(val):
                return val
        return "N/A"

    # Non-taxonomy fields
    for source in (frag, verb):
        if source:
            val = deep_get(source, field)
            if valid(val):
                return val
    if occ:
        val = occ.get(field)
        if valid(val):
            return val
    return "N/A"


# Uses GBIF's species/match endpoint with fuzzy=true to catch OCR/AI name typos.
# Only surfaces a badge when GBIF had to fuzzy-match and confidence >= 85%.

_GBIF_FUZZY_CACHE: dict = {}
_GBIF_FUZZY_HEADERS = {"User-Agent": "HerbAudit/1.0 (herbarium specimen evaluation)"}


def _gbif_fuzzy_resolve(name: str):
    """
    Query GBIF fuzzy name matching for the closest accepted canonical name.
    Returns a suggestion only when GBIF matched fuzzily (the input had a typo/variant)
    and confidence >= 85%.
    Returns (matched_name, accepted_name, confidence_0_to_1) or (None, None, 0.0).
    """
    clean = strip_authors(name)
    if not clean or clean == "na" or " " not in clean:
        return None, None, 0.0

    if clean in _GBIF_FUZZY_CACHE:
        return _GBIF_FUZZY_CACHE[clean]

    try:
        r = _gbif_get(
            "https://api.gbif.org/v1/species/match",
            params={"name": clean, "fuzzy": "true"},
            headers=_GBIF_FUZZY_HEADERS,
            timeout=10,
        ).json()
    except Exception:
        # Don't cache a transient network failure as a permanent "no match".
        return None, None, 0.0

    match_type = r.get("matchType", "NONE")
    confidence = r.get("confidence", 0) / 100.0
    canonical  = r.get("canonicalName", "") or ""

    # Only show a badge when GBIF had to fuzzy-match (name wasn't exact)
    if match_type != "FUZZY" or not canonical:
        _GBIF_FUZZY_CACHE[clean] = (None, None, 0.0)
        return None, None, 0.0

    result = (canonical, canonical, confidence)
    _GBIF_FUZZY_CACHE[clean] = result
    return result


_GBIF_MATCH_CACHE: dict = {}


def _gbif_match(name: str):
    """
    Query GBIF's own backbone (species/match) for *name*. Unlike _gbif_fuzzy_resolve,
    this is passed the raw name INCLUDING authorship — GBIF's match scoring uses
    authorship similarity to disambiguate homonyms (e.g. two different "Salix livida"
    published by different authors resolve to different, unrelated accepted names).

    Returns a dict with status/canonical/usageKey/acceptedUsageKey, or None if GBIF
    has no confident species-rank-or-below match (falls back to a bare genus match,
    or nothing at all).
    """
    if not name or not name.strip():
        return None
    if name in _GBIF_MATCH_CACHE:
        return _GBIF_MATCH_CACHE[name]

    try:
        r = _gbif_get(
            "https://api.gbif.org/v1/species/match",
            params={"name": _fix_author_case(name)},
            headers=_GBIF_FUZZY_HEADERS, timeout=10,
        ).json()
    except Exception as exc:
        # A network/timeout failure is not the same as GBIF confidently
        # saying "no match" — leave it uncached so a later call retries
        # instead of reusing a stale failure.
        print(f"  GBIF backbone lookup error for '{name}': {exc}")
        return None

    result = None
    if r.get("matchType") not in ("NONE", None) and r.get("rank") not in (None, "GENUS", "FAMILY", "ORDER"):
        result = {
            "status":           r.get("status"),
            "canonical":        r.get("canonicalName"),
            "usageKey":         r.get("usageKey"),
            "acceptedUsageKey": r.get("acceptedUsageKey"),
            "matchType":        r.get("matchType"),
            "rank":             r.get("rank"),
        }

    _GBIF_MATCH_CACHE[name] = result
    return result


def _gbif_match_with_authorship_retry(name: str):
    """_gbif_match(name), retried with authorship stripped if the full name
    finds nothing — the authorship itself may be OCR garble even when the
    species name is real. Shared by check_gbif_backbone_fallback and the
    scoring module's NOREL check, which both need this same fallback."""
    result = _gbif_match(name)
    if result is None:
        stripped = strip_authors(name)
        if stripped and stripped != name:
            result = _gbif_match(stripped)
    return result


def check_gbif_backbone_fallback(target_name, ai_name):
    """
    GBIF-backbone equivalent of check_wfo_fallback — used when WFO can't
    resolve a taxonomy mismatch (down, rate-limited, or the name isn't
    indexed there). Looks both names up against GBIF's own backbone
    taxonomy (the same source the "truth" record itself comes from) and flags whether
    the mismatch is a synonym relationship rather than a genuinely different species.

    Badge prefixes mirror WFO's: GBIFbb-rev (only GBIF's name is outdated),
    GBIFbb-sib (both names are outdated synonyms of the same accepted taxon),
    GBIFbb (only the AI's name is outdated).
    """
    clean_target = strip_authors(target_name)
    clean_ai     = strip_authors(ai_name)
    if not clean_target or not clean_ai or clean_ai == "na":
        return False, ""

    t = _gbif_match(target_name)
    a = _gbif_match_with_authorship_retry(ai_name)
    if t is None or a is None:
        return False, ""

    # AI's name must be a byte-exact GBIF match, not a typo GBIF fuzzy-fixed.
    if a.get("matchType") != "EXACT":
        return False, ""

    t_is_syn = t["status"] == "SYNONYM"
    a_is_syn = a["status"] == "SYNONYM"

    t_accepted_key = t["acceptedUsageKey"] if t_is_syn else t["usageKey"]
    a_accepted_key = a["acceptedUsageKey"] if a_is_syn else a["usageKey"]
    if not t_accepted_key or not a_accepted_key or t_accepted_key != a_accepted_key:
        return False, ""

    if t_is_syn and a_is_syn:
        return True, (
            f"GBIFbb-sib||GBIF name '{clean_target}' is outdated; "
            f"AI name '{clean_ai}' is also a synonym of the same accepted taxon"
        )
    if t_is_syn:
        return True, (
            f"GBIFbb-rev||GBIF name '{clean_target}' is outdated — "
            f"AI correctly gave accepted name '{clean_ai}'"
        )
    if a_is_syn:
        return True, (
            f"GBIFbb||'{clean_ai}' is a synonym of accepted GBIF name '{clean_target}'"
        )
    # Neither name is a synonym, yet both resolve to the SAME accepted usageKey
    # — likely an orthographic/gender-agreement spelling variant of one epithet.
    return True, (
        f"GBIFbb-eq||'{clean_ai}' and '{clean_target}' both resolve to the same "
        f"accepted GBIF taxon — likely a spelling/orthographic variant of the same name"
    )


def check_label_synonym(ai_name, truth_row):
    """Check if the AI name appears verbatim in the GBIF verbatim or interpreted blobs."""
    if truth_row is None or not ai_name:
        return False, ""

    clean_ai = strip_authors(ai_name).lower()
    if len(clean_ai) < 2:
        return False, ""

    verbatim_blob    = str(truth_row.get("truth_verbatim",       "")).lower()
    interpreted_name = str(truth_row.get("truth_scientificName", "")).lower()

    if clean_ai in verbatim_blob or clean_ai in interpreted_name:
        return True, "GBIF||Verified via Verbatim/Interpreted match"

    return False, ""
