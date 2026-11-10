"""What GBIF knows about a collector: where and when the collector's preserved specimens were collected.

An experiment, not wired into HerbAudit yet. collector_profile() asks GBIF about one collector name and
caches the answer; judge() compares a profile with the country and year HerbAudit read from a label.
GBIF only knows digitized specimens, and the same name can belong to several people, so a verdict is a
hint for a person to look at, never a correction.

Names are matched loosely: "Bamps, P.", "P. Bamps", "Bamps P" and "Bamps" are one collector. GBIF compares
the whole recordedBy string, so every common spelling of the person is sent in one request (the values are OR'd).
Pooling spellings only adds countries and years to a profile, so it makes a flag less likely, never more.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import requests

_SEARCH = "https://api.gbif.org/v1/occurrence/search"
_HEADERS = {"User-Agent": "herbaudit-research"}
DEFAULT_CACHE = Path.home() / ".herbaudit" / "gbif_collectors.json"

_lock = threading.Lock()
_net_lock = threading.Lock()
_caches: dict[str, dict] = {}
_offline = False          # set after the first failed lookup so an offline run is not slowed down by retries

_SPLIT = re.compile(r"\s*(?:;|&|/|\band\b|\bet\b|\bet al\.?|\bs\.?\s?n\.?\b)\s*", re.IGNORECASE)
_INITIALS = re.compile(r"^(?:[^\W\d_]\.?){1,4}$")          # "P." "K.H." "KH" "J.M"


def _cache_for(path: Path) -> dict:
    key = str(path)
    if key not in _caches:
        try:
            _caches[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _caches[key] = {}
    return _caches[key]


def _letters(token: str) -> list[str]:
    """Initials in a token: "K.H." -> K, H; "Pierre" -> P."""
    t = token.strip()
    if not t:
        return []
    if _INITIALS.match(t) and (("." in t) or len(t) <= 2 or t.isupper()):
        return [c for c in t if c.isalpha()]
    return [t[0]]


def parse_name(name: str) -> tuple[str, list[str]]:
    """(surname, initials) of the first collector in a recordedBy string. Initials may be empty."""
    first = _SPLIT.split((name or "").strip(), maxsplit=1)[0].strip(" ,.")
    if not first:
        return "", []
    if "," in first:
        surname, rest = first.split(",", 1)
        initials = [c for tok in rest.split() for c in _letters(tok)]
        return surname.strip(" ."), initials
    tokens = first.split()
    if len(tokens) == 1:
        return tokens[0].strip("."), []
    given = [t for t in tokens if _INITIALS.match(t) and ("." in t or len(t) <= 2 or t.isupper())]
    if given and len(given) < len(tokens):
        surname = " ".join(t for t in tokens if t not in given)
        return surname.strip(" ."), [c for t in given for c in _letters(t)]
    return tokens[-1].strip("."), [c for t in tokens[:-1] for c in _letters(t)]


def name_variants(name: str) -> list[str]:
    """Every common way the same person's name is written. GBIF's recordedBy filter is case-sensitive, so the
    surname is tried as written and in Title case ("BAMPS" and "bamps" are both common on labels)."""
    surname, initials = parse_name(name)
    if not surname:
        return []
    surnames = {surname, surname.title()}
    out = set(surnames)
    if initials:
        initials = [i.upper() for i in initials]
        forms = {"".join(i + "." for i in initials), " ".join(i + "." for i in initials),
                 ".".join(initials), ". ".join(initials), " ".join(initials), "".join(initials)}
        for s in surnames:
            for f in forms:
                out.update({f"{s}, {f}", f"{s} {f}", f"{f} {s}"})
    return sorted(out)


def _profile_key(name: str) -> str | None:
    surname, initials = parse_name(name)
    return f"{surname.lower()}|{''.join(initials).lower()}" if surname else None


def collector_profile(name: str, cache_path: Path | None = None) -> dict | None:
    """{"total": n, "countries": {ISO2: n}, "years": {year: n}} for preserved specimens whose recordedBy is
    any common spelling of `name`, or None if GBIF could not be reached."""
    global _offline
    key = _profile_key(name)
    if not key:
        return None
    path = Path(cache_path) if cache_path else DEFAULT_CACHE
    with _lock:
        cache = _cache_for(path)
        if key in cache:
            return cache[key]
    if _offline:
        return None
    params = {"recordedBy": name_variants(name), "basisOfRecord": "PRESERVED_SPECIMEN", "limit": 0,
              "facet": ["country", "year"], "facetLimit": 500}
    r = None
    with _net_lock:                                   # one request at a time: GBIF rate-limits bursts
        for wait in (0, 3, 10):
            time.sleep(wait)
            try:
                r = requests.get(_SEARCH, headers=_HEADERS, timeout=30, params=params).json()
            except (requests.RequestException, ValueError):
                r = None
            if isinstance(r, dict) and "count" in r:
                break
        else:
            _offline = True
            return None
        time.sleep(0.15)
    facets = {f["field"]: {c["name"]: c["count"] for c in f.get("counts", [])}
              for f in r.get("facets", []) if isinstance(f, dict)}
    profile = {"total": int(r.get("count", 0)),
               "countries": facets.get("COUNTRY", {}),
               "years": {int(y): n for y, n in facets.get("YEAR", {}).items()}}
    with _lock:
        cache[key] = profile
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return profile


def judge(profile: dict | None, country: str | None = None, year: int | None = None, *,
          min_records: int = 20, year_margin: int = 5, minus: dict | None = None) -> dict:
    """Compare a profile with the country (ISO 3166 alpha-2) and year read from a label.

    verdict: "unreachable" (GBIF could not be asked), "unknown" (too few records to say anything),
    "country" (many records, none in that country), "year" (the year is well outside the years the
    collector's records cover, ignoring the extreme 5% at each end), or "ok".
    `minus` removes one specimen from the counts, {"country": ISO2, "year": int}; it is how the specimen
    being tested is kept out of its own evidence when testing on specimens that are themselves on GBIF."""
    if profile is None:
        return {"verdict": "unreachable", "total": None}
    total = profile["total"] - (1 if minus else 0)
    countries = dict(profile["countries"])
    years = {int(y): n for y, n in profile["years"].items()}      # JSON turns the cached year keys into text
    if minus:
        if minus.get("country") in countries:
            countries[minus["country"]] -= 1
        if minus.get("year") in years:
            years[minus["year"]] -= 1
    out = {"total": total, "in_country": countries.get(country, 0) if country else None}
    if total < min_records:
        return {**out, "verdict": "unknown"}
    if country and countries.get(country, 0) <= 0:
        return {**out, "verdict": "country"}
    if year:
        expanded = sorted(y for y, n in years.items() for _ in range(max(n, 0)))
        if expanded:
            lo = expanded[int(0.05 * (len(expanded) - 1))]
            hi = expanded[int(0.95 * (len(expanded) - 1))]
            out["year_range"] = (lo, hi)
            if year < lo - year_margin or year > hi + year_margin:
                return {**out, "verdict": "year"}
    return {**out, "verdict": "ok"}


def collector_flag(ai_row, own: dict | None = None) -> dict | None:
    """What HerbAudit shows from this module: a "warn" flag when the collector HerbAudit read has no
    preserved specimen on GBIF from the country HerbAudit read, and a quiet "ok" note when it has.
    Returns {"kind", "text", "detail"} or None (collector unknown to GBIF, or nothing to compare).
    Only the country is flagged; the year check had too many false alarms (15% of correct years), so the
    year only appears in the tooltip of the "ok" note.

    `own` is the specimen's own GBIF record ({"recordedBy", "countryCode", "year"}) when the specimen is
    itself on GBIF (the evaluation against GBIF); it is left out of the evidence, otherwise the specimen
    would vouch for itself. Never raises: any failure just means no flag. Set HERBAUDIT_NO_COLLECTOR_CHECK=1
    to turn it off."""
    import os
    if os.environ.get("HERBAUDIT_NO_COLLECTOR_CHECK"):
        return None
    try:
        def get(field):
            v = str(ai_row.get(field, "") or "").strip()
            return "" if v.lower() in ("nan", "none", "n/a") else v
        collector, country = get("recordedBy"), get("country")
        if not collector or not country:
            return None
        from .bdq import _country_code
        iso = _country_code(country)
        if not iso:
            return None
        profile = collector_profile(collector)
        minus = None
        if profile and own and str(own.get("recordedBy") or "") in name_variants(collector):
            minus = {"country": own.get("countryCode"), "year": own.get("year")}
        ym = re.match(r"\s*(\d{4})", get("eventDate"))
        year = int(ym.group(1)) if ym else None
        verdict = judge(profile, iso, year, minus=minus)
        if verdict["verdict"] in ("ok", "year"):          # consistent: say so, quietly (the year only in the tooltip)
            detail = (f"GBIF has {verdict['in_country']:,} of {verdict['total']:,} preserved specimens by "
                      f"'{collector}' from {country} ({iso}).")
            if verdict.get("year_range"):
                lo, hi = verdict["year_range"]
                inside = lo - 5 <= year <= hi + 5 if year else None
                detail += f" Usual collecting years on GBIF: {lo}-{hi}"
                detail += f"; the label year {year} is {'inside' if inside else 'outside'} that range." if year else "."
            return {"kind": "ok", "text": f"collector recorded in {country}", "detail": detail}
        if verdict["verdict"] != "country":
            return None
        counts = dict(profile["countries"])
        if minus and minus.get("country") in counts:
            counts[minus["country"]] -= 1                 # the specimen itself is not evidence about itself
        top = sorted(((c, n) for c, n in counts.items() if n > 0), key=lambda kv: -kv[1])[:4]
        where = ", ".join(f"{c} {n:,}" for c, n in top) or "none"
        return {"kind": "warn", "text": f"collector not recorded in {country}",
                "detail": f"GBIF has {verdict['total']:,} preserved specimens by '{collector}', "
                          f"none from {country} ({iso}). Most are from: {where}. "
                          "A hint to check the collector and country, not a correction."}
    except Exception:
        return None


def flag_badge_html(flag: dict | None) -> str:
    """Small amber badge for a report card header; empty string when there is no flag."""
    if not flag:
        return ""
    import html
    ok = flag.get("kind") == "ok"
    bg, fg, border, mark = ("#dcfce7", "#15803d", "#bbf7d0", "✓") if ok else ("#fef3c7", "#92400e", "#fde68a", "⚠")
    return (f"<span title=\"{html.escape(flag['detail'], quote=True)}\" style='font-size:0.6rem; "
            f"background:{bg}; color:{fg}; border:1px solid {border}; border-radius:99px; "
            f"padding:2px 8px; margin-left:6px; font-weight:600;'>{mark} {html.escape(flag['text'])}</span>")
