"""World Flora Online (WFO) name resolution / synonym fallback."""
from __future__ import annotations

import json
from pathlib import Path

import requests
import Levenshtein
from unidecode import unidecode

from .text import strip_authors
from .gbif import check_gbif_backbone_fallback


_WFO_ENABLED = True

_WFO_CACHE: dict = {}   # clean binomial → (accepted_clean_binomial, accepted_path, is_synonym)
_WFO_CACHE_FILE = Path(".wfo_cache.json")

_WFO_HEADERS = {
    "User-Agent": "HerbAudit/1.0 (herbarium specimen evaluation)"
}

# list.worldfloraonline.org's server never sends its intermediate
# certificate, so plain TLS verification fails with "unable to get local
# issuer certificate". Fix: pin the missing intermediate ourselves
# (wfo_intermediate.pem, a public CA cert, safe to commit) on top of
# certifi's normal root bundle.
#
# If this starts failing again (WFO rotates issuers), re-pin from the leaf
# cert's AIA "CA Issuers" URL:
#   openssl s_client -connect list.worldfloraonline.org:443 \
#       -servername list.worldfloraonline.org -showcerts
_WFO_INTERMEDIATE_CERT_FILE = Path(__file__).parent / "wfo_intermediate.pem"


_WFO_CA_BUNDLE_FILE = Path.home() / ".herbaudit" / "wfo_ca_bundle.pem"
_wfo_ca_bundle_path: str | None = None


def _wfo_ca_bundle() -> str:
    """Build (once, cached to disk) a CA bundle = certifi roots + WFO's missing intermediate."""
    global _wfo_ca_bundle_path
    if _wfo_ca_bundle_path is not None:
        return _wfo_ca_bundle_path
    import certifi
    if not _WFO_CA_BUNDLE_FILE.exists():
        _WFO_CA_BUNDLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        roots = Path(certifi.where()).read_text(encoding="utf-8")
        intermediate = _WFO_INTERMEDIATE_CERT_FILE.read_text(encoding="utf-8")
        _WFO_CA_BUNDLE_FILE.write_text(roots + "\n" + intermediate, encoding="utf-8")
    _wfo_ca_bundle_path = str(_WFO_CA_BUNDLE_FILE)
    return _wfo_ca_bundle_path


def _load_wfo_cache():
    """Load persisted WFO results from disk, covering both _WFO_CACHE
    (matching_rest.php existence check) and _WFO_CB_CACHE (Checklistbank
    accepted/synonym status, used by _wfo_checklistbank_match).

    File format is {"wfo": {...}, "checklistbank": {...}}; an older flat
    cache file (just the _WFO_CACHE dict directly) is detected by the
    absence of both keys and self-heals into the new format on next save.
    """
    if not _WFO_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(_WFO_CACHE_FILE.read_text(encoding="utf-8"))
        if "wfo" in raw or "checklistbank" in raw:
            wfo_data, cb_data = raw.get("wfo", {}), raw.get("checklistbank", {})
        else:
            wfo_data, cb_data = raw, {}   # old flat-format file
        for k, v in wfo_data.items():
            _WFO_CACHE[k] = tuple(v) if v is not None else (None, None, False)
        for k, v in cb_data.items():
            _WFO_CB_CACHE[k] = v
        print(f"  WFO cache loaded ({len(_WFO_CACHE)} entries, "
              f"{len(_WFO_CB_CACHE)} Checklistbank entries)")
    except Exception:
        pass  # corrupt cache — start fresh


def _save_wfo_cache():
    """Persist both WFO caches to disk for the next run (see _load_wfo_cache)."""
    try:
        data = {
            "wfo": {k: list(v) for k, v in _WFO_CACHE.items()},
            "checklistbank": _WFO_CB_CACHE,
        }
        _WFO_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _wfo_resolve(name: str, authorship: str = "", allow_fuzzy: bool = True):
    """Look up *name* on WFO and return (accepted_clean_binomial,
    accepted_path, is_synonym), or (None, None, False) if WFO has no match
    or the request fails. One request per unique name.

    *authorship*, when given, is included in the query — WFO's
    matching_rest.php only returns an exact "match" for a byte-exact
    name+author hit; without it a genuine name can miss here while still
    resolving in _wfo_lookup's separate existence check.

    *allow_fuzzy*: False skips the 0.85-similarity candidate fallback,
    requiring a byte-exact hit. Pass False when resolving the AI's own
    extracted text, so a typo can't silently pass as a real taxon.
    """
    clean = strip_authors(name)
    if not clean or clean == "na" or " " not in clean:
        return None, None, False

    authorship = (authorship or "").strip()
    query      = f"{clean} {authorship}".strip() if authorship else clean
    cache_key  = f"{clean}|{authorship}|{'f' if allow_fuzzy else 'x'}"

    if cache_key in _WFO_CACHE:
        return _WFO_CACHE[cache_key]

    def _cache_fail():
        _WFO_CACHE[cache_key] = (None, None, False)
        return None, None, False

    try:
        r = requests.get(
            "https://list.worldfloraonline.org/matching_rest.php",
            params={"input_string": query},
            headers=_WFO_HEADERS, timeout=10, verify=_wfo_ca_bundle(),
        ).json()

        if r.get("error"):
            return _cache_fail()

        match = r.get("match")
        if not match and allow_fuzzy:
            # WFO only returns "match" for a byte-exact hit (name + author).
            # Any OCR/AI typo, stray/extra author, or spacing quirk instead
            # lands the right taxon in "candidates" with match=null. Fall
            # back to the closest candidate, same 0.85 similarity bar used
            # for the GBIF fuzzy-name fallback elsewhere in this file.
            for cand in (r.get("candidates") or []):
                cand_clean = strip_authors(cand.get("full_name_plain", ""))
                if not cand_clean or not cand.get("placement"):
                    continue
                sim = 1.0 - (Levenshtein.distance(clean, cand_clean) / max(len(clean), len(cand_clean), 1))
                if sim >= 0.85:
                    match = cand
                    break

        if not match:
            return _cache_fail()

        placement = match.get("placement", "") or ""
        is_synonym = "$" in placement
        accepted_path = placement.split("$", 1)[0] if is_synonym else placement

        segs = [s for s in accepted_path.split("/") if s]
        if len(segs) < 2:
            return _cache_fail()
        accepted_clean = unidecode(" ".join(segs[-2:]).lower())

        result = (accepted_clean, accepted_path, is_synonym)
        _WFO_CACHE[cache_key] = result
        if accepted_clean != clean:
            _WFO_CACHE[f"{accepted_clean}||{'f' if allow_fuzzy else 'x'}"] = (accepted_clean, accepted_path, False)
        return result

    except Exception as exc:
        print(f"  WFO lookup error for '{clean}': {exc}")
        return _cache_fail()


_WFO_LOOKUP_CACHE: dict = {}   # clean binomial → (found, hierarchy, closest_name, closest_similarity)


def _wfo_lookup(name: str, authorship: str = ""):
    """Look up *name* on WFO — existence + hierarchy only, no accepted/
    synonym claim (matching_rest.php has no status field for that; see the
    _WFO_ENABLED comment above).

    *authorship*, when given, is included in the query to help disambiguate
    homonyms (e.g. "Quercus robur L." vs "Quercus robur Asso"); it's
    strictly better-or-equal to an author-less query, never worse.

    Returns (found, hierarchy, closest_name, closest_similarity):
      found              — True only on a byte-exact WFO match (name+author).
      hierarchy          — '/'-joined placement path when found, else None.
      closest_name       — best fuzzy candidate when there's no exact match,
                            honestly labeled as a guess.
      closest_similarity — that candidate's name-similarity score, 0-1.
    """
    clean = strip_authors(name)
    if not clean or clean == "na" or " " not in clean:
        return False, None, None, None

    authorship = (authorship or "").strip()
    query      = f"{clean} {authorship}".strip() if authorship else clean
    # Different authors for the same bare name must not share a cache entry —
    # they can be genuinely different homonyms with different WFO results.
    cache_key  = f"{clean}|{authorship}"

    if cache_key in _WFO_LOOKUP_CACHE:
        return _WFO_LOOKUP_CACHE[cache_key]

    def _cache(result):
        _WFO_LOOKUP_CACHE[cache_key] = result
        return result

    try:
        r = requests.get(
            "https://list.worldfloraonline.org/matching_rest.php",
            params={"input_string": query},
            headers=_WFO_HEADERS, timeout=10, verify=_wfo_ca_bundle(),
        ).json()

        if r.get("error"):
            return _cache((False, None, None, None))

        match = r.get("match")
        if match:
            hierarchy = (match.get("placement", "") or "").replace("$", " → ") or None
            return _cache((True, hierarchy, None, None))

        # No exact match — report the closest candidate as a labeled guess,
        # not as a resolved result. Lower bar than the old 0.85 (which existed
        # to silently stand in for a real match); here we're just showing the
        # user the nearest thing WFO has, so a lower-confidence hint is still
        # useful as long as it's honestly scored. Compared name-only (clean),
        # not query-with-author, so a correct name isn't penalized just
        # because the author didn't line up with this particular candidate.
        best_name, best_sim = None, 0.0
        for cand in (r.get("candidates") or []):
            cand_clean = strip_authors(cand.get("full_name_plain", ""))
            if not cand_clean:
                continue
            sim = 1.0 - (Levenshtein.distance(clean, cand_clean) / max(len(clean), len(cand_clean), 1))
            if sim > best_sim:
                best_name, best_sim = cand.get("full_name_plain", cand_clean), sim

        if best_name and best_sim >= 0.5:
            return _cache((False, None, best_name, round(best_sim, 3)))
        return _cache((False, None, None, None))

    except Exception as exc:
        print(f"  WFO lookup error for '{clean}': {exc}")
        return _cache((False, None, None, None))


def _wfo_paths_related(path_a: str, path_b: str) -> bool:
    """True if two WFO classification paths are identical, or one is exactly
    one rank deeper than the other along the same lineage (e.g. garbled
    trailing authorship can land the path resolver one rank deeper than the
    clean truth name did). Deliberately not a general shared-prefix check —
    that would falsely relate a genus-level resolution to every species
    beneath it.
    """
    if path_a == path_b:
        return True
    parts_a, parts_b = path_a.split("/"), path_b.split("/")
    if abs(len(parts_a) - len(parts_b)) != 1:
        return False
    shorter, longer = (parts_a, parts_b) if len(parts_a) < len(parts_b) else (parts_b, parts_a)
    return longer[:len(shorter)] == shorter


def check_wfo_fallback(target_name, ai_name):
    """
    Determines whether a taxonomy mismatch is a nomenclature difference
    rather than a real error, using WFO's classification placement instead
    of an explicit synonym list.

    Badge prefixes: WFO-rev / WFO-sib / WFO.
    """
    clean_target = strip_authors(target_name)
    clean_ai     = strip_authors(ai_name)

    if not clean_target or not clean_ai or clean_ai == "na":
        return False, ""

    t_accepted, t_path, t_is_syn = _wfo_resolve(target_name)
    if t_accepted is None:
        return False, ""   # WFO couldn't resolve GBIF's name — skip

    # AI gave the current accepted name, GBIF's name is an outdated synonym
    if t_is_syn and clean_ai == t_accepted:
        return True, (
            f"WFO-rev||GBIF name '{clean_target}' is outdated — "
            f"AI correctly gave accepted name '{t_accepted}'"
        )

    # allow_fuzzy=False: the AI's name must be a real taxon, not a typo
    # that happens to resolve nearby.
    a_accepted, a_path, _ = _wfo_resolve(ai_name, allow_fuzzy=False)
    if a_accepted is not None and _wfo_paths_related(a_path, t_path):
        if t_is_syn:
            return True, (
                f"WFO-sib||GBIF name '{clean_target}' is outdated; "
                f"AI name '{clean_ai}' is also a synonym of '{t_accepted}'"
            )
        else:
            return True, (
                f"WFO||'{clean_ai}' is a synonym of accepted GBIF name '{clean_target}'"
            )

    return False, ""


# Separate from every synonym-relationship check above: answers a simpler
# question — does WFO itself consider the AI's extracted name accepted or a
# synonym, regardless of relationship to the GBIF truth name. Shown as its
# own badge, never affects scoring or exclusion.
#
# Queried via GBIF's Checklistbank (dataset 2004, WFO's own checklist),
# whose nameusage/search has a taxonomicStatus field that WFO's own
# matching_rest.php lacks.
_WFO_CB_DATASET = 2004
_WFO_CB_CACHE: dict = {}


def _wfo_checklistbank_match(name: str):
    """Return {"status": "accepted"|"synonym"|..., "accepted":
    accepted_name_or_None} for *name* per WFO's own checklist, or None if
    WFO has no confident match.

    Tries the full name first, then backs off one trailing word at a time
    down to just the genus (a single word is an acceptable match — some
    real GBIF truth values are genus-only, e.g. "Hornstedtia Retz."). Each
    attempt re-queries Checklistbank and requires every word of that prefix
    to appear in the returned match. Backing off word-by-word and letting
    WFO's own match confirm each attempt avoids relying on a fixed text rule
    to tell an author citation apart from an old-style capitalized eponym
    epithet — both can look identical ("capitalized 2nd word") but mean
    different things depending on the name.
    """
    raw_words = [w.strip(",;:") for w in str(name).strip().split() if w.strip(",;:")]
    if not raw_words:
        return None

    for n in range(len(raw_words), 0, -1):
        prefix = raw_words[:n]
        query = " ".join(prefix)
        cache_key = query.lower()
        if cache_key in _WFO_CB_CACHE:
            cached = _WFO_CB_CACHE[cache_key]
            if cached is not None:
                return cached
            continue

        result = None
        try:
            r = requests.get(
                f"https://api.checklistbank.org/dataset/{_WFO_CB_DATASET}/nameusage/search",
                params={"q": query, "limit": 1},
                timeout=10,
            ).json()
            results = r.get("result") or []
            if results:
                usage = results[0].get("usage", {})
                status = usage.get("status")
                accepted_obj = usage.get("accepted") or {}
                accepted_name = accepted_obj.get("name", {}).get("scientificName")
                # /nameusage/search is relevance-ranked full-text search, not
                # an exact-name lookup — it returns its best partial hit even
                # when the query has words it couldn't place. Require every
                # word of this prefix to appear in the matched name first.
                matched_words = set(
                    unidecode((usage.get("name") or {}).get("scientificName", "")).lower().split()
                )
                query_words = {unidecode(w.lower()) for w in prefix}
                if status and query_words <= matched_words:
                    result = {"status": status, "accepted": accepted_name}
        except Exception as exc:
            print(f"  WFO (Checklistbank) lookup error for '{query}': {exc}")

        _WFO_CB_CACHE[cache_key] = result
        if result is not None:
            return result

    return None


def wfo_status_badge_html(name: str) -> str:
    """Small standalone HTML badge showing WFO's own accepted/synonym opinion
    on *name*, independent of any relationship to a GBIF truth name. Returns
    "" when WFO has no confident match, so callers can splice it in freely."""
    info = _wfo_checklistbank_match(name)
    if info is None:
        return ""
    status = (info.get("status") or "").lower()
    if status == "accepted":
        fg, bg, label = "#15803d", "#dcfce7", "WFO: accepted"
    elif status == "synonym":
        acc = info.get("accepted") or "?"
        fg, bg, label = "#a16207", "#fef3c7", f"WFO: synonym of <i>{acc}</i>"
    else:
        fg, bg, label = "#6b7280", "#f3f4f6", f"WFO: {status or 'unrecognized'}"
    tooltip = "Independent status from World Flora Online for the AI-extracted name, regardless of relationship to the GBIF truth name"
    return (
        f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
        f"font-size:0.6rem; font-weight:700; background:{bg}; color:{fg};' "
        f"title='{tooltip}'>{label}</span>"
    )


# Informational/non-scoring: when a scientificName mismatch isn't rescued as
# a synonym, this does a best-effort fuzzy lookup against WFO's candidate
# list purely to annotate the mismatch row (did the AI read a real, if
# wrong, species name, or is it OCR garble?) — never affects scoring.

_NEAREST_TAXON_CACHE: dict = {}


def _nearest_taxon(name: str):
    """
    Return (source, closest_known_name, similarity) for *name* via WFO's fuzzy
    candidate list, or (None, None, 0.0) if WFO has nothing close.
    """
    clean = strip_authors(name)
    if not clean or clean == "na" or " " not in clean:
        return None, None, 0.0

    if clean in _NEAREST_TAXON_CACHE:
        return _NEAREST_TAXON_CACHE[clean]

    def _best(candidates: list[str]):
        best_name, best_sim = None, 0.0
        for cand in candidates:
            if not cand:
                continue
            sim = 1.0 - (Levenshtein.distance(clean, cand) / max(len(clean), len(cand), 1))
            if sim > best_sim:
                best_name, best_sim = cand, sim
        return best_name, best_sim

    result = (None, None, 0.0)
    try:
        r = requests.get(
            "https://list.worldfloraonline.org/matching_rest.php",
            params={"input_string": clean},
            headers=_WFO_HEADERS, timeout=10, verify=_wfo_ca_bundle(),
        ).json()
        cands = list(r.get("candidates") or [])
        if r.get("match"):
            cands.append(r["match"])
        names = [strip_authors(c.get("full_name_plain", "")) for c in cands]
        best_name, best_sim = _best(names)
        if best_name:
            result = ("WFO", best_name, best_sim)
    except Exception:
        pass

    _NEAREST_TAXON_CACHE[clean] = result
    return result


def check_synonym_fallback(target_name, ai_name):
    """Combined nomenclature check: WFO is tried first (when enabled — see
    _WFO_ENABLED), and GBIF's own backbone taxonomy is the fallback when WFO
    can't resolve it. GBIF's verbatim/interpreted record is checked
    separately, as a distinct signal, by the caller."""
    if _WFO_ENABLED:
        is_wfo, wfo_note = check_wfo_fallback(target_name, ai_name)
        if is_wfo:
            return is_wfo, wfo_note
    return check_gbif_backbone_fallback(target_name, ai_name)
