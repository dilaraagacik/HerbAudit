"""TDWG BDQ (Biodiversity Data Quality) tests, run on the AI transcription with --bdq_test.

Each test follows its specification in the official test list
(https://github.com/tdwg/bdq/blob/master/tg2/core/TG2_tests.csv, readable at
https://bdq.tdwg.org/) and returns a BDQ response: status, result and comment.
Only the tests whose inputs HerbAudit extracts and that need no online lookup are here.

The test functions take a record keyed by Darwin Core term without the "dwc:" prefix,
exactly as BDQ defines them. HerbAudit-specific clean-up happens in prepare_record().
To add a test: write a function `(record, params) -> response` and add a BdqTest to BDQ_TESTS.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import bdq_dates, gbif
from .text import strip_authors

RUN_HAS_RESULT = "RUN_HAS_RESULT"
INTERNAL_PREREQUISITES_NOT_MET = "INTERNAL_PREREQUISITES_NOT_MET"
EXTERNAL_PREREQUISITES_NOT_MET = "EXTERNAL_PREREQUISITES_NOT_MET"
AMENDED = "AMENDED"
NOT_AMENDED = "NOT_AMENDED"
FILLED_IN = "FILLED_IN"
NOT_REPORTED = "NOT_REPORTED"                # a measure that could not run
COMPLIANT = "COMPLIANT"
NOT_COMPLIANT = "NOT_COMPLIANT"


@dataclass(frozen=True)
class BdqTest:
    label: str
    guid: str
    version: str                       # DateLastUpdated of the specification
    run: Callable[[dict, dict], dict]
    defaults: dict = field(default_factory=dict)   # BDQ Parameters and their default values


# --- Responses and shared helpers ---------------------------------------------------

def _result(compliant: bool, comment: str) -> dict:
    return {"status": RUN_HAS_RESULT, "result": COMPLIANT if compliant else NOT_COMPLIANT, "comment": comment}


def _cannot_run(comment: str) -> dict:
    return {"status": INTERNAL_PREREQUISITES_NOT_MET, "result": "", "comment": comment}


def is_empty(value) -> bool:
    """bdqval:Empty: missing, or only whitespace. The text "null" is NOT empty in BDQ."""
    return value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == ""


def _number(value) -> float | None:
    """The value as a finite number, or None (e.g. "41.05 N" or "")."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --- ISO 8601 dates --------------------------------------------------------------------
# A date is YYYY, YYYY-MM, YYYY-MM-DD or YYYY-DDD (day of year), optionally followed by a
# time (Thh, Thh:mm, Thh:mm:ss[.s]) and a time zone (Z, +hh, +hhmm, +hh:mm).
# An interval is two of those joined by "/" whose end is not before its start.

_TIME = r"(T([01]\d|2[0-3])(:[0-5]\d(:[0-5]\d([.,]\d+)?)?)?(Z|[+-]([01]\d|2[0-3])(:?[0-5]\d)?)?)?"
_ISO_DATE = re.compile(rf"(\d{{4}})(-(\d{{2}})(-(\d{{2}}))?|-(\d{{3}}))?{_TIME}")


def _date_span(text: str) -> tuple[date, date] | None:
    """First and last day of a single ISO 8601 date, or None if it is not one."""
    m = _ISO_DATE.fullmatch(text.strip())
    if not m:
        return None
    year, month, day, ordinal = m.group(1), m.group(3), m.group(5), m.group(6)
    try:
        y = int(year)
        if ordinal:
            first = date(y, 1, 1) + timedelta(days=int(ordinal) - 1)
            return (first, first) if first.year == y and int(ordinal) > 0 else None
        if day:
            d = date(y, int(month), int(day))      # rejects 1963-02-29
            return d, d
        if month:
            first = date(y, int(month), 1)
            nxt = date(y + (first.month == 12), first.month % 12 + 1, 1)
            return first, nxt - timedelta(days=1)
        return date(y, 1, 1), date(y, 12, 31)
    except ValueError:                             # month 13, day 32, year 0000
        return None


def iso_span(text) -> tuple[date, date] | None:
    """First and last day covered by an ISO 8601 date or interval, or None if not valid."""
    if is_empty(text):
        return None
    parts = str(text).strip().split("/")
    if len(parts) > 2:
        return None
    spans = [_date_span(p) for p in parts]
    if None in spans:
        return None
    start, end = spans[0][0], spans[-1][1]
    return (start, end) if start <= end else None


def _param_date(value, today: date) -> date:
    """A Parameter value: an ISO date, or the defaults "{current year}" / "{current day}"."""
    text = str(value).strip()
    if text == "{current year}":
        return date(today.year, 12, 31)
    if text == "{current day}":
        return today
    span = iso_span(text)
    if span is None:
        raise ValueError(f"Not an ISO 8601 date: {value!r}")
    return span[0]


# --- NOTEMPTY tests -----------------------------------------------------------------

def _not_empty(term: str):
    def test(record: dict, params: dict) -> dict:
        if is_empty(record.get(term)):
            return _result(False, f"dwc:{term} is bdqval:Empty")
        return _result(True, f"dwc:{term} is bdqval:NotEmpty")
    return test


def _any_not_empty(terms: tuple[str, ...], what: str):
    def test(record: dict, params: dict) -> dict:
        filled = [t for t in terms if not is_empty(record.get(t))]
        if filled:
            return _result(True, ", ".join(f"dwc:{t}" for t in filled) + " bdqval:NotEmpty")
        return _result(False, f"No {what} term is bdqval:NotEmpty")
    return test


EVENT_TEMPORAL_TERMS = (
    "eventDate", "year", "month", "day", "startDayOfYear", "endDayOfYear", "verbatimEventDate",
)
LOCATION_TERMS = (
    "higherGeographyID", "higherGeography", "continent", "country", "countryCode", "stateProvince",
    "county", "municipality", "waterBody", "island", "islandGroup", "locality", "locationID",
    "verbatimLocality", "decimalLatitude", "decimalLongitude", "verbatimCoordinates",
    "verbatimLatitude", "verbatimLongitude", "footprintWKT",
)
TAXON_TERMS = (
    "taxonID", "scientificNameID", "acceptedNameUsageID", "parentNameUsageID", "originalNameUsageID",
    "taxonConceptID", "scientificName", "higherClassification", "kingdom", "phylum", "class", "order",
    "superfamily", "family", "subfamily", "tribe", "subtribe", "genus", "genericName", "subgenus",
    "infragenericEpithet", "specificEpithet", "infraspecificEpithet", "vernacularName", "cultivarEpithet",
)


def validate_country_notempty(record: dict, params: dict) -> dict:
    country, code = record.get("country"), record.get("countryCode")
    if not is_empty(country):
        return _result(True, "dwc:country is bdqval:NotEmpty")
    if str(code).strip() == "XZ":
        return _result(True, "dwc:country is bdqval:Empty and dwc:countryCode is XZ (high seas)")
    return _result(False, "dwc:country is bdqval:Empty")


# --- Dates ------------------------------------------------------------------------------

def _date_standard(term: str):
    def test(record: dict, params: dict) -> dict:
        value = record.get(term)
        if is_empty(value):
            return _cannot_run(f"dwc:{term} is bdqval:Empty")
        if iso_span(value):
            return _result(True, f"dwc:{term} is a valid ISO 8601 date")
        return _result(False, f"dwc:{term} is not a valid ISO 8601 date")
    return test


def validate_eventdate_inrange(record: dict, params: dict) -> dict:
    value = record.get("eventDate")
    if is_empty(value):
        return _cannot_run("dwc:eventDate is bdqval:Empty")
    span = iso_span(value)
    if span is None:
        return _cannot_run("dwc:eventDate is not a valid ISO 8601 date")
    earliest = _param_date(params["bdqval:earliestValidDate"], params["today"])
    latest = _param_date(params["bdqval:latestValidDate"], params["today"])
    return _result(earliest <= span[0] and span[1] <= latest,
                   f"dwc:eventDate must be within {earliest.isoformat()} to {latest.isoformat()}")


def validate_dateidentified_inrange(record: dict, params: dict) -> dict:
    value, event_date = record.get("dateIdentified"), record.get("eventDate")
    include_event = str(params["bdqval:includeEventDate"]).strip().lower() == "true"
    if is_empty(value):
        return _cannot_run("dwc:dateIdentified is bdqval:Empty")
    identified = iso_span(value)
    if identified is None:
        return _cannot_run("dwc:dateIdentified is not a valid ISO 8601 date")
    event = None
    if include_event and not is_empty(event_date):
        event = iso_span(event_date)
        if event is None:
            return _cannot_run("dwc:eventDate is not a valid ISO 8601 date")
    earliest = _param_date(params["bdqval:earliestValidDate"], params["today"])
    latest = _param_date(params["bdqval:latestValidDate"], params["today"])
    if not (earliest <= identified[0] and identified[1] <= latest):
        return _result(False, f"dwc:dateIdentified is not within {earliest.isoformat()} to {latest.isoformat()}")
    if event and identified[1] < event[0]:
        return _result(False, "dwc:dateIdentified is before dwc:eventDate")
    return _result(True, "dwc:dateIdentified is in range")


# --- Date amendments and duration ---------------------------------------------------
# These follow FilteredPush event_date_qc v3.0.5 (the BDQ reference implementation),
# using its date parser translated in bdq_dates.py, so they give the same answers.

def _amendment(status: str, comment: str, term: str | None = None, value: str | None = None) -> dict:
    return {"status": status, "result": f"dwc:{term}={value}" if term else "", "comment": comment}


def amend_eventdate_from_verbatim(record: dict, params: dict) -> dict:
    event_date, verbatim = record.get("eventDate"), record.get("verbatimEventDate")
    if not bdq_dates.is_empty(event_date):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:eventDate is bdqval:NotEmpty, not changing")
    if bdq_dates.is_empty(verbatim):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:verbatimEventDate is bdqval:Empty")
    state, value = bdq_dates.extract_date_from_verbatim(verbatim)
    if state in (bdq_dates.DATE, bdq_dates.RANGE):
        return _amendment(FILLED_IN, f"dwc:eventDate filled in from dwc:verbatimEventDate [{verbatim}]",
                          "eventDate", value)
    if state == bdq_dates.AMBIGUOUS:
        return _amendment(NOT_AMENDED, f"dwc:verbatimEventDate [{verbatim}] is ambiguous")
    if state == bdq_dates.SUSPECT:
        return _amendment(NOT_AMENDED, f"Interpretation of dwc:verbatimEventDate [{verbatim}] is suspect")
    return _amendment(INTERNAL_PREREQUISITES_NOT_MET, f"Unable to extract a date from [{verbatim}]")


def amend_eventdate_standardized(record: dict, params: dict) -> dict:
    event_date = record.get("eventDate")
    if bdq_dates.event_date_valid(event_date):
        return _amendment(NOT_AMENDED, "dwc:eventDate is already a valid ISO 8601 date")
    if bdq_dates.is_empty(event_date):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:eventDate is bdqval:Empty")
    state, value = bdq_dates.extract_date_from_verbatim(event_date)
    if state in (bdq_dates.DATE, bdq_dates.RANGE):
        return _amendment(AMENDED, f"dwc:eventDate [{event_date}] interpreted as [{value}]", "eventDate", value)
    if state == bdq_dates.AMBIGUOUS:
        return _amendment(NOT_AMENDED, f"Interpretation of dwc:eventDate [{event_date}] is ambiguous")
    if state == bdq_dates.SUSPECT:
        # The Java leaves the status unset here; the specification says "otherwise NOT_AMENDED".
        return _amendment(NOT_AMENDED, f"Interpretation of dwc:eventDate [{event_date}] is suspect")
    return _amendment(NOT_AMENDED, f"Unable to extract a date from dwc:eventDate [{event_date}]")


def amend_dateidentified_standardized(record: dict, params: dict) -> dict:
    value = record.get("dateIdentified")
    if bdq_dates.event_date_valid(value):
        return _amendment(NOT_AMENDED, "dwc:dateIdentified is already a valid ISO 8601 date")
    if bdq_dates.is_empty(value):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:dateIdentified is bdqval:Empty")
    state, extracted = bdq_dates.extract_date_from_verbatim(value)
    if state in (bdq_dates.DATE, bdq_dates.RANGE):
        return _amendment(AMENDED, f"dwc:dateIdentified [{value}] interpreted as [{extracted}]",
                          "dateIdentified", extracted)
    if state == bdq_dates.AMBIGUOUS:
        return _amendment(NOT_AMENDED, f"Interpretation of dwc:dateIdentified [{value}] as [{extracted}] is ambiguous")
    if state == bdq_dates.SUSPECT:
        return _amendment(NOT_AMENDED, f"Interpretation of dwc:dateIdentified [{value}] as [{extracted}] is suspect")
    # yyyy-dd-mm written for yyyy-mm-dd, e.g. 2021-28-10
    if re.fullmatch(r"[0-9]{4}.[0-9]{2}.[0-9]{2}", value) and int(value[5:7]) > 12 and int(value[8:]) < 12:
        swapped = bdq_dates.extract_interval(f"{value[0:4]}-{value[8:]}-{value[5:7]}")
        if swapped is not None and swapped.is_single_day():
            return _amendment(AMENDED, f"dwc:dateIdentified [{value}] read as year-day-month",
                              "dateIdentified", str(swapped))
    return _amendment(NOT_AMENDED, f"Unable to extract a date from dwc:dateIdentified [{value}]")


def amend_event_from_eventdate(record: dict, params: dict) -> dict:
    """Fill the empty ones of year, month, day, startDayOfYear, endDayOfYear from dwc:eventDate.
    HerbAudit does not extract these terms, so for a valid date this proposes all that apply."""
    event_date = record.get("eventDate")
    if is_empty(event_date):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:eventDate is bdqval:Empty")
    span = iso_span(event_date)
    if span is None:
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, f"dwc:eventDate [{event_date}] is not a valid ISO 8601-1 date")
    start, end = span
    proposed = {"year": start.year if start.year == end.year else None,
                "month": start.month if (start.year, start.month) == (end.year, end.month) else None,
                "day": start.day if start == end else None,
                "startDayOfYear": start.timetuple().tm_yday, "endDayOfYear": end.timetuple().tm_yday}
    changes = {term: value for term, value in proposed.items() if value is not None and is_empty(record.get(term))}
    if not changes:
        return _amendment(NOT_AMENDED, f"No empty year, month, day, startDayOfYear or endDayOfYear to fill from dwc:eventDate [{event_date}]")
    return {"status": AMENDED, "result": "; ".join(f"dwc:{term}={value}" for term, value in changes.items()),
            "comment": f"dwc:eventDate [{event_date}] contains values for {', '.join('dwc:' + t for t in changes)}"}


def measure_eventdate_duration(record: dict, params: dict) -> dict:
    event_date = record.get("eventDate")
    if bdq_dates.is_empty(event_date):
        return {**_cannot_run("dwc:eventDate is bdqval:Empty"), "result": NOT_REPORTED}
    try:
        seconds = bdq_dates.measure_duration_seconds(event_date)
    except (bdq_dates.DateTimeParseError, ValueError):
        return {**_cannot_run(f"dwc:eventDate [{event_date}] is not a valid ISO 8601 date"), "result": NOT_REPORTED}
    return {"status": RUN_HAS_RESULT, "result": str(seconds),
            "comment": f"dwc:eventDate [{event_date}] spans {seconds} seconds"}


# --- Names found in the GBIF Backbone Taxonomy (online) ----------------------------
# These reuse HerbAudit's own GBIF species/match lookup (gbif.gbif_species_match), so a
# name the audit already checked costs no extra request. GBIF's matchType says whether
# the name matched exactly or only fuzzily; BDQ "found" means an exact match.

GBIF_BACKBONE = "GBIF Backbone Taxonomy"


def _gbif_unavailable(params: dict) -> dict | None:
    authority = str(params.get("bdqval:sourceAuthority", GBIF_BACKBONE)).strip()
    if authority != GBIF_BACKBONE:
        return {"status": EXTERNAL_PREREQUISITES_NOT_MET, "result": "",
                "comment": f"bdqval:sourceAuthority [{authority}] is not available; only {GBIF_BACKBONE} is"}
    return None


def _gbif_unreachable() -> dict:
    return {"status": EXTERNAL_PREREQUISITES_NOT_MET, "result": "", "comment": "GBIF API not reachable"}


def _is_exact(match: dict) -> bool:
    """An exact match, or several equally exact ones (homonyms), which GBIF reports as NONE."""
    return match.get("matchType") == "EXACT" or (
        match.get("matchType") == "NONE" and "Multiple equal matches" in str(match.get("note", "")))


def _name_match(name: str) -> dict | None:
    # Same request as the audit's name check (run.py → gbif._gbif_fuzzy_resolve), so it is cached.
    return gbif.gbif_species_match(strip_authors(name) or name, fuzzy="true")


def validate_scientificname_found(record: dict, params: dict) -> dict:
    name = record.get("scientificName")
    if (unavailable := _gbif_unavailable(params)):
        return unavailable
    if is_empty(name):
        return _cannot_run("dwc:scientificName is bdqval:Empty")
    match = _name_match(str(name).strip())
    if match is None:
        return _gbif_unreachable()
    if _is_exact(match):
        return _result(True, f"dwc:scientificName found in the {GBIF_BACKBONE}")
    return _result(False, f"dwc:scientificName was not found in the {GBIF_BACKBONE}")


def validate_genus_found(record: dict, params: dict) -> dict:
    genus = record.get("genus")
    if (unavailable := _gbif_unavailable(params)):
        return unavailable
    if is_empty(genus):
        return _cannot_run("dwc:genus is bdqval:Empty")
    genus = str(genus).strip()
    found = _result(True, f"dwc:genus found at the rank of genus in the {GBIF_BACKBONE}")
    # Free when the full name was already matched exactly: GBIF's answer includes its genus.
    if not is_empty(record.get("scientificName")):
        name_match = _name_match(str(record["scientificName"]).strip())
        if name_match and name_match.get("matchType") == "EXACT" and name_match.get("genus") == genus:
            return found
    match = gbif.gbif_species_match(genus, rank="GENUS", strict="true")
    if match is None:
        return _gbif_unreachable()
    if match.get("matchType") == "EXACT" and match.get("rank") == "GENUS" or (
            _is_exact(match) and match.get("matchType") == "NONE"):
        return found
    return _result(False, f"dwc:genus does not have an equivalent at the rank of genus in the {GBIF_BACKBONE}")


# --- Coordinates and elevation ------------------------------------------------------

def _in_range(term: str, low_param: str | None, high_param: str | None, low=None, high=None):
    def test(record: dict, params: dict) -> dict:
        value = record.get(term)
        if is_empty(value):
            return _cannot_run(f"dwc:{term} is bdqval:Empty")
        number = _number(value)
        if number is None:
            return _cannot_run(f"dwc:{term} is not a number")
        lo = float(params[low_param]) if low_param else low
        hi = float(params[high_param]) if high_param else high
        return _result(lo <= number <= hi, f"dwc:{term} must be between {lo:g} and {hi:g}")
    return test


def validate_coordinates_notzero(record: dict, params: dict) -> dict:
    lat, lon = _number(record.get("decimalLatitude")), _number(record.get("decimalLongitude"))
    if lat is None or lon is None:
        return _cannot_run("dwc:decimalLatitude or dwc:decimalLongitude is bdqval:Empty or not a number")
    if lat == 0 and lon == 0:
        return _result(False, "dwc:decimalLatitude and dwc:decimalLongitude are both zero")
    return _result(True, "dwc:decimalLatitude and dwc:decimalLongitude are not both zero")


def validate_minelevation_lessthan_maxelevation(record: dict, params: dict) -> dict:
    low = _number(record.get("minimumElevationInMeters"))
    high = _number(record.get("maximumElevationInMeters"))
    if low is None or high is None:
        return _cannot_run("dwc:minimumElevationInMeters or dwc:maximumElevationInMeters is bdqval:Empty or not a number")
    return _result(low <= high, "dwc:minimumElevationInMeters must not be greater than dwc:maximumElevationInMeters")


# --- Country and coordinates against country boundaries ----------------------------------
# These follow FilteredPush geo_ref_qc (the BDQ reference implementation), with one difference:
# its default boundaries are Natural Earth admin-1 UNION Marine Regions EEZs, here only the
# Natural Earth admin-0 land borders are available (files/countries.json.gz, simplified to ~200 m,
# well below the 3 km buffer). A point just off the coast of its country can therefore fail.
# shapely and the boundaries are loaded on first use, so runs without --bdq_test never pay for them.
# HerbAudit does not extract dwc:countryCode, so it is looked up from dwc:country (exact names only).

NE_COUNTRIES = "Natural Earth 10m admin-0 countries"
_COUNTRY_FILE = Path(__file__).with_name("files") / "countries.json.gz"
_NAME_FIELDS = ("NAME", "NAME_LONG", "ADMIN", "FORMAL_EN", "NAME_SORT")


@lru_cache(maxsize=1)
def _boundaries():
    """(country shapes, their ISO 3166 alpha-2 codes, the lower-case names of each, spatial index)."""
    import gzip
    import json
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    with gzip.open(_COUNTRY_FILE, "rt", encoding="utf-8") as f:
        features = json.load(f)
    shapes = [shape(feature["g"]) for feature in features]
    codes = [feature["p"].get("ISO_A2_EH") or feature["p"].get("ISO_A2") for feature in features]
    names = [{str(feature["p"].get(k) or "").strip().lower() for k in _NAME_FIELDS} - {""} for feature in features]
    return shapes, codes, names, STRtree(shapes)


def _country_known(country: str) -> bool:
    return any(country.strip().lower() in names for names in _boundaries()[2])


def _country_code(country: str) -> str | None:
    """ISO 3166 alpha-2 code for an exact country name, or None."""
    key = country.strip().lower()
    for code, names in zip(_boundaries()[1], _boundaries()[2]):
        if key in names and code and code != "-99":
            return code
    from hdx.location.country import Country
    iso3 = Country.get_iso3_country_code(country.strip())
    return Country.get_iso2_from_iso3(iso3) if iso3 else None


def _near_country(code: str, lat: float, lon: float, buffer_km: float) -> bool:
    """Is the point within buffer_km of the country? The buffer is converted to degrees at 111 km
    per degree, as geo_ref_qc does, so it is approximate away from the equator."""
    from shapely.geometry import Point
    shapes, codes, _, tree = _boundaries()
    area = Point(lon, lat).buffer(buffer_km / 111)
    return any(codes[i] == code for i in tree.query(area, predicate="intersects"))


def _boundaries_unavailable(params: dict) -> dict | None:
    authority = str(params.get("bdqval:sourceAuthority", NE_COUNTRIES)).strip()
    if authority != NE_COUNTRIES:
        return {"status": EXTERNAL_PREREQUISITES_NOT_MET, "result": "",
                "comment": f"bdqval:sourceAuthority [{authority}] is not available; only {NE_COUNTRIES} is"}
    return None


def _buffer_km(params: dict) -> float:
    return (_number(params.get("bdqval:spatialBufferInMeters")) or 3000) / 1000


def validate_country_found(record: dict, params: dict) -> dict:
    country = record.get("country")
    if (unavailable := _boundaries_unavailable(params)):
        return unavailable
    if is_empty(country):
        return _cannot_run("dwc:country is bdqval:Empty")
    if _country_known(str(country)):
        return _result(True, f"dwc:country [{country}] exists as a country name in {NE_COUNTRIES}")
    return _result(False, f"dwc:country [{country}] is not a country name in {NE_COUNTRIES}")


def _coordinates_and_code(record: dict):
    """(lat, lon, code, None), or (None, None, None, the reason the test cannot run)."""
    lat_text, lon_text, country = record.get("decimalLatitude"), record.get("decimalLongitude"), record.get("country")
    if is_empty(lat_text):
        return None, None, None, "dwc:decimalLatitude is bdqval:Empty"
    if is_empty(lon_text):
        return None, None, None, "dwc:decimalLongitude is bdqval:Empty"
    code = str(record.get("countryCode") or "").strip().upper() or None
    if code is None:
        if is_empty(country):
            return None, None, None, "dwc:country is bdqval:Empty"
        code = _country_code(str(country))
        if code is None:
            return None, None, None, f"dwc:country [{country}] could not be matched to a country code"
    lat, lon = _number(lat_text), _number(lon_text)
    if lat is None or lon is None:
        return None, None, None, "dwc:decimalLatitude or dwc:decimalLongitude is not a number"
    return lat, lon, code, None


def validate_coordinates_countrycode_consistent(record: dict, params: dict) -> dict:
    if (unavailable := _boundaries_unavailable(params)):
        return unavailable
    lat, lon, code, problem = _coordinates_and_code(record)
    if problem:
        return _cannot_run(problem)
    where = f"dwc:country [{record.get('country') or code}] ({code})"
    if _near_country(code, lat, lon, _buffer_km(params)):
        return _result(True, f"dwc:decimalLatitude and dwc:decimalLongitude lie within the bounds of {where}")
    return _result(False, f"dwc:decimalLatitude and dwc:decimalLongitude lie outside the bounds of {where}")


def _flipped(text: str) -> str:
    text = text.strip()
    return text.replace("-", "") if "-" in text else "-" + text


def amend_coordinates_transposed(record: dict, params: dict) -> dict:
    if (unavailable := _boundaries_unavailable(params)):
        return _amendment(EXTERNAL_PREREQUISITES_NOT_MET, unavailable["comment"])
    lat, lon, code, problem = _coordinates_and_code(record)
    if problem:
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, problem)
    km = 3.0       # fixed in geo_ref_qc, there is no buffer Parameter
    where = f"dwc:country [{record.get('country') or code}] ({code})"
    lat_text, lon_text = str(record["decimalLatitude"]).strip(), str(record["decimalLongitude"]).strip()
    if _near_country(code, lat, lon, km):
        return _amendment(NOT_AMENDED, f"dwc:decimalLatitude and dwc:decimalLongitude lie within the bounds of {where}")
    # In geo_ref_qc's order: swapped (only if the longitude can be a latitude), then each sign flipped.
    attempts = [(lon_text, lat_text)] if abs(lon) <= 90 else []
    attempts += [(_flipped(lat_text), lon_text), (lat_text, _flipped(lon_text)), (_flipped(lat_text), _flipped(lon_text))]
    for new_lat, new_lon in attempts:
        if _near_country(code, float(new_lat), float(new_lon), km):
            return {"status": AMENDED, "result": f"dwc:decimalLatitude={new_lat}; dwc:decimalLongitude={new_lon}",
                    "comment": f"dwc:decimalLatitude and dwc:decimalLongitude lie outside the bounds of {where}; "
                               f"transposed or sign-flipped, they lie within it"}
    return _amendment(NOT_AMENDED, f"dwc:decimalLatitude and dwc:decimalLongitude lie outside the bounds of {where}, "
                                   f"and no transposition or sign change places them inside it")


def amend_countrycode_from_coordinates(record: dict, params: dict) -> dict:
    """Propose dwc:countryCode from the coordinates. Unlike geo_ref_qc this cannot tell the high seas
    (code XZ) from a gap in the land borders, so a point outside every country is not amended."""
    if (unavailable := _boundaries_unavailable(params)):
        return _amendment(EXTERNAL_PREREQUISITES_NOT_MET, unavailable["comment"])
    if not is_empty(record.get("countryCode")):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:countryCode is bdqval:NotEmpty, not changing")
    lat, lon = _number(record.get("decimalLatitude")), _number(record.get("decimalLongitude"))
    if lat is None or lon is None:
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET,
                          "dwc:decimalLatitude or dwc:decimalLongitude is bdqval:Empty or not a number")
    from shapely.geometry import Point
    _, codes, _, tree = _boundaries()
    found = {codes[i] for i in tree.query(Point(lon, lat), predicate="intersects")} - {None, "-99"}
    if len(found) != 1:
        return _amendment(NOT_AMENDED, "No unique country found containing dwc:decimalLatitude and dwc:decimalLongitude")
    code = found.pop()
    return _amendment(FILLED_IN, f"dwc:decimalLatitude and dwc:decimalLongitude lie within the country {code}",
                      "countryCode", code)


# --- Names ------------------------------------------------------------------------------

def validate_polynomial_consistent(record: dict, params: dict) -> dict:
    name = record.get("scientificName")
    parts = {t: str(record.get(t) or "").strip() for t in ("genericName", "specificEpithet", "infraspecificEpithet")}
    if is_empty(name):
        return _cannot_run("dwc:scientificName is bdqval:Empty")
    if not any(parts.values()):
        return _cannot_run("dwc:genericName, dwc:specificEpithet and dwc:infraspecificEpithet are bdqval:Empty")
    # Words of the name, without rank markers, qualifiers ("?", "aff.") and authorship
    # (authorship starts at the first capitalised word after the genus, or at "(").
    words = []
    for i, word in enumerate(str(name).split()):
        if i and (word[:1].isupper() or word.startswith("(")):
            break
        if i and (word.endswith(".") or word == "?" or word in ("x", "×")):
            continue
        words.append(word)
    problems = []
    if parts["genericName"] and (not words or words[0] != parts["genericName"]):
        problems.append("dwc:genericName")
    if parts["specificEpithet"] and (len(words) < 2 or words[1] != parts["specificEpithet"]):
        problems.append("dwc:specificEpithet")
    if parts["infraspecificEpithet"] and (len(words) < 3 or words[-1] != parts["infraspecificEpithet"]):
        problems.append("dwc:infraspecificEpithet")
    if problems:
        return _result(False, f"dwc:scientificName does not match {', '.join(problems)}")
    return _result(True, "dwc:scientificName is consistent with its name parts")


# --- Family, type status and elevation read from the label --------------------------

def validate_family_found(record: dict, params: dict) -> dict:
    family = record.get("family")
    if (unavailable := _gbif_unavailable(params)):
        return unavailable
    if is_empty(family):
        return _cannot_run("dwc:family is bdqval:Empty")
    match = gbif.gbif_species_match(str(family).strip(), rank="FAMILY", strict="true")
    if match is None:
        return _gbif_unreachable()
    if (match.get("matchType") == "EXACT" and match.get("rank") == "FAMILY") or (
            _is_exact(match) and match.get("matchType") == "NONE"):
        return _result(True, f"dwc:family has an equivalent at the rank of family in the {GBIF_BACKBONE}")
    return _result(False, f"dwc:family does not have an equivalent at the rank of family in the {GBIF_BACKBONE}")


def validate_taxon_unambiguous(record: dict, params: dict) -> dict:
    """Does dwc:scientificName resolve to one taxon? Uses the audit's cached name match, which follows
    the DiSSCo reference: an exact or fuzzy match with a usageKey and confidence of at least 80."""
    name = record.get("scientificName")
    if (unavailable := _gbif_unavailable(params)):
        return unavailable
    if is_empty(name):
        return _cannot_run("dwc:scientificName is bdqval:Empty")
    match = _name_match(str(name).strip())
    if match is None:
        return _gbif_unreachable()
    if match.get("usageKey") and match.get("matchType") in ("EXACT", "FUZZY") and (match.get("confidence") or 0) >= 80:
        return _result(True, f"dwc:scientificName resolves to a single taxon in the {GBIF_BACKBONE} "
                            f"({match['matchType'].lower()} match, confidence {match['confidence']})")
    if _is_exact(match) and match.get("matchType") == "NONE":
        return _result(False, f"dwc:scientificName matches more than one taxon in the {GBIF_BACKBONE}")
    return _result(False, f"dwc:scientificName cannot be resolved to a single taxon in the {GBIF_BACKBONE}")


def validate_classification_consistent(record: dict, params: dict) -> dict:
    """Do dwc:family and dwc:genus agree with GBIF's classification of dwc:scientificName?
    For a synonym GBIF answers with the accepted name's classification, which can legitimately differ
    from the label (an old genus or family), so a mismatch there cannot be judged."""
    if (unavailable := _gbif_unavailable(params)):
        return unavailable
    terms = {t: str(record[t]).strip() for t in ("family", "genus") if not is_empty(record.get(t))}
    if not terms:
        return _cannot_run("dwc:family and dwc:genus are bdqval:Empty")
    if is_empty(record.get("scientificName")):
        return _cannot_run("dwc:scientificName is bdqval:Empty")
    match = _name_match(str(record["scientificName"]).strip())
    if match is None:
        return _gbif_unreachable()
    if not match.get("usageKey"):
        return _cannot_run(f"dwc:scientificName was not matched in the {GBIF_BACKBONE}, so its classification is unknown")
    own_genus = str(match.get("canonicalName") or "").split(" ")[0]
    allowed = {"family": {str(match.get("family") or "").lower()}, "genus": {str(match.get("genus") or "").lower(), own_genus.lower()}}
    wrong = [t for t, value in terms.items() if value.lower() not in allowed[t]]
    if not wrong:
        return _result(True, f"{', '.join('dwc:' + t for t in terms)} consistent with dwc:scientificName in the {GBIF_BACKBONE}")
    if match.get("status") != "ACCEPTED":
        return _cannot_run(f"dwc:scientificName is not an accepted name in the {GBIF_BACKBONE}, so "
                           f"{', '.join('dwc:' + t for t in wrong)} cannot be judged against it")
    return _result(False, f"{', '.join('dwc:' + t for t in wrong)} not consistent with dwc:scientificName in the {GBIF_BACKBONE} "
                         f"(family {match.get('family')}, genus {match.get('genus')})")


TYPE_STATUS_VOCABULARY = "GBIF TypeStatus Vocabulary"
# The concepts of https://api.gbif.org/v1/vocabularies/TypeStatus (51 terms, fetched 2026-10-23).
TYPE_STATUS_TERMS = (
    "Isosyntype", "Isoparatype", "Isoneotype", "Isolectotype", "Isoepitype", "Iconotype", "Hypotype",
    "Hapantotype", "Extype", "Exsyntype", "Exparatype", "Exneotype", "Exlectotype", "Exisotype", "Exholotype",
    "Exepitype", "Epitype", "Cotype", "Allotype", "Alloneotype", "Allolectotype", "Paratopotype", "TypeSeries",
    "Homeotype", "Metatype", "Clonotype", "TypeStrain", "Topotype", "SupplementaryType", "SecondaryType",
    "Plesiotype", "Plastotype", "Plastosyntype", "Plastoparatype", "Plastoneotype", "Plastolectotype",
    "Plastoisotype", "Plastoholotype", "Paraneotype", "Paralectotype", "Holotype", "Paratype", "Syntype",
    "Neotype", "Lectotype", "Isotype", "OriginalMaterial", "NotAType", "TypeGenus", "TypeSpecies", "Type",
)


def _type_status_unavailable(params: dict) -> dict | None:
    authority = str(params.get("bdqval:sourceAuthority", TYPE_STATUS_VOCABULARY)).strip()
    if authority != TYPE_STATUS_VOCABULARY:
        return {"status": EXTERNAL_PREREQUISITES_NOT_MET, "result": "",
                "comment": f"bdqval:sourceAuthority [{authority}] is not available; only {TYPE_STATUS_VOCABULARY} is"}
    return None


def validate_typestatus_standard(record: dict, params: dict) -> dict:
    value = record.get("typeStatus")
    if (unavailable := _type_status_unavailable(params)):
        return unavailable
    if is_empty(value):
        return _cannot_run("dwc:typeStatus is bdqval:Empty")
    if value != value.strip() or not value.isprintable():
        return _result(False, "dwc:typeStatus has leading or trailing whitespace or non-printing characters")
    # The first word of each |-delimited portion must be a term of the vocabulary.
    firsts = [(portion.split() or [""])[0] for portion in value.split("|")]
    if all(word in TYPE_STATUS_TERMS for word in firsts):
        return _result(True, f"dwc:typeStatus found in the {TYPE_STATUS_VOCABULARY}")
    return _result(False, f"dwc:typeStatus not found in the {TYPE_STATUS_VOCABULARY}")


def _standard_type_status(word: str) -> str | None:
    """The vocabulary term a word stands for: the term itself in any case, or an
    abbreviation that starts exactly one term ("Holo." -> Holotype, "Epi." -> Epitype)."""
    stem = word.rstrip(".").lower()
    for term in TYPE_STATUS_TERMS:
        if term.lower() == stem:
            return term
    starts = [t for t in TYPE_STATUS_TERMS if len(stem) >= 3 and word.endswith(".") and t.lower().startswith(stem)]
    return starts[0] if len(starts) == 1 else None


def amend_typestatus_standardized(record: dict, params: dict) -> dict:
    value = record.get("typeStatus")
    if (unavailable := _type_status_unavailable(params)):
        return unavailable
    if is_empty(value):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:typeStatus is bdqval:Empty")
    portions = []
    for portion in value.split("|"):
        words = portion.split()
        term = _standard_type_status(words[0]) if words else None
        if term is None:
            return _amendment(NOT_AMENDED, f"dwc:typeStatus not found in the {TYPE_STATUS_VOCABULARY}")
        portions.append(re.sub(r"\S+", lambda _: term, portion, count=1))   # keep the original spacing
    amended = "|".join(portions)
    if amended == value:
        return _amendment(NOT_AMENDED, "dwc:typeStatus is already standard")
    return _amendment(AMENDED, f"dwc:typeStatus contains an interpretable value in the {TYPE_STATUS_VOCABULARY}",
                      "typeStatus", amended)


# To meters. The conversion keeps every digit, as the specification asks (10 yards -> 9.144).
_METERS_PER_UNIT = {"m": Decimal("1"), "meter": Decimal("1"), "meters": Decimal("1"),
                    "metre": Decimal("1"), "metres": Decimal("1"),
                    "km": Decimal("1000"), "kilometer": Decimal("1000"), "kilometers": Decimal("1000"),
                    "kilometre": Decimal("1000"), "kilometres": Decimal("1000"),
                    "ft": Decimal("0.3048"), "foot": Decimal("0.3048"), "feet": Decimal("0.3048"),
                    "yd": Decimal("0.9144"), "yard": Decimal("0.9144"), "yards": Decimal("0.9144")}
_NUMBER = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNITS = "|".join(sorted(_METERS_PER_UNIT, key=len, reverse=True))
_VERBATIM_ELEVATION = re.compile(
    rf"(?:(?:min(?:imum)?|max(?:imum)?)\.?\s+)?(?:elev(?:ation)?|alt(?:itude)?)?\.?\s*[:=]?\s*"
    rf"{_NUMBER}(?:\s*(?:-|–|—|to)\s*{_NUMBER})?\s*({_UNITS})\.?",
    re.IGNORECASE)


def _meters(number: str, unit: str) -> str:
    meters = Decimal(number.replace(",", "")) * _METERS_PER_UNIT[unit.lower()]
    return format(meters.normalize(), "f")


def amend_elevation_from_verbatim(record: dict, params: dict) -> dict:
    verbatim = record.get("verbatimElevation")
    low, high = record.get("minimumElevationInMeters"), record.get("maximumElevationInMeters")
    if is_empty(verbatim):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET, "dwc:verbatimElevation is bdqval:Empty")
    if not is_empty(low) or not is_empty(high):
        return _amendment(INTERNAL_PREREQUISITES_NOT_MET,
                          "dwc:minimumElevationInMeters or dwc:maximumElevationInMeters is bdqval:NotEmpty")
    m = _VERBATIM_ELEVATION.fullmatch(str(verbatim).strip())
    if not m:
        return _amendment(NOT_AMENDED, f"dwc:verbatimElevation [{verbatim}] contains an ambiguous value")
    first, last, unit = m.group(1), m.group(2) or m.group(1), m.group(3)
    if Decimal(first.replace(",", "")) > Decimal(last.replace(",", "")):
        return _amendment(NOT_AMENDED, f"dwc:verbatimElevation [{verbatim}] contains an ambiguous value")
    return {"status": FILLED_IN,
            "result": f"dwc:minimumElevationInMeters={_meters(first, unit)}; dwc:maximumElevationInMeters={_meters(last, unit)}",
            "comment": f"dwc:verbatimElevation [{verbatim}] contains an interpretable value"}


# --- The test list ----------------------------------------------------------------------

_ELEVATION_DEFAULTS = {"bdqval:minimumValidElevationInMeters": "-430", "bdqval:maximumValidElevationInMeters": "8850"}

BDQ_TESTS = [
    # Event (dates)
    BdqTest("VALIDATION_EVENTTEMPORAL_NOTEMPTY", "41267642-60ff-4116-90eb-499fee2cd83f", "2023-09-30",
            _any_not_empty(EVENT_TEMPORAL_TERMS, "Event temporal")),
    BdqTest("VALIDATION_EVENTDATE_NOTEMPTY", "f51e15a6-a67d-4729-9c28-3766299d2985", "2023-09-17",
            _not_empty("eventDate")),
    BdqTest("VALIDATION_EVENTDATE_STANDARD", "4f2bf8fd-fc5c-493f-a44c-e7b16153c803", "2024-09-16",
            _date_standard("eventDate")),
    BdqTest("VALIDATION_EVENTDATE_INRANGE", "3cff4dc4-72e9-4abe-9bf3-8a30f1618432", "2024-09-16",
            validate_eventdate_inrange,
            {"bdqval:earliestValidDate": "1582-11-15", "bdqval:latestValidDate": "{current year}"}),
    BdqTest("AMENDMENT_EVENTDATE_FROM_VERBATIM", "6d0a0c10-5e4a-4759-b448-88932f399812", "2024-09-16",
            amend_eventdate_from_verbatim),
    BdqTest("AMENDMENT_EVENTDATE_STANDARDIZED", "718dfc3c-cb52-4fca-b8e2-0e722f375da7", "2024-09-16",
            amend_eventdate_standardized),
    BdqTest("AMENDMENT_EVENT_FROM_EVENTDATE", "710fe118-17e1-440f-b428-88ba3f547d6d", "2024-09-16",
            amend_event_from_eventdate),
    BdqTest("MEASURE_EVENTDATE_DURATIONINSECONDS", "56b6c695-adf1-418e-95d2-da04cad7be53", "2024-09-16",
            measure_eventdate_duration),
    # Identification
    BdqTest("VALIDATION_DATEIDENTIFIED_STANDARD", "66269bdd-9271-4e76-b25c-7ab81eebe1d8", "2024-09-16",
            _date_standard("dateIdentified")),
    BdqTest("VALIDATION_DATEIDENTIFIED_INRANGE", "dc8aae4b-134f-4d75-8a71-c4186239178e", "2024-09-16",
            validate_dateidentified_inrange,
            {"bdqval:earliestValidDate": "1753-01-01", "bdqval:latestValidDate": "{current day}",
             "bdqval:includeEventDate": "true"}),
    BdqTest("AMENDMENT_DATEIDENTIFIED_STANDARDIZED", "39bb2280-1215-447b-9221-fd13bc990641", "2024-09-16",
            amend_dateidentified_standardized),
    # Location
    BdqTest("VALIDATION_LOCATION_NOTEMPTY", "58486cb6-1114-4a8a-ba1e-bd89cfe887e9", "2023-09-18",
            _any_not_empty(LOCATION_TERMS, "Location")),
    BdqTest("VALIDATION_COUNTRY_NOTEMPTY", "6ce2b2b4-6afe-4d13-82a0-390d31ade01c", "2024-09-27",
            validate_country_notempty),
    BdqTest("VALIDATION_COUNTRY_FOUND", "69b2efdc-6269-45a4-aecb-4cb99c2ae134", "2024-08-19",
            validate_country_found, {"bdqval:sourceAuthority": NE_COUNTRIES}),
    BdqTest("VALIDATION_COORDINATESCOUNTRYCODE_CONSISTENT", "adb27d29-9f0d-4d52-b760-a77ba57a69c9", "2024-08-30",
            validate_coordinates_countrycode_consistent,
            {"bdqval:sourceAuthority": NE_COUNTRIES, "bdqval:spatialBufferInMeters": "3000"}),
    BdqTest("AMENDMENT_COORDINATES_TRANSPOSED", "f2b4a50a-6b2f-4930-b9df-da87b6a21082", "2024-11-11",
            amend_coordinates_transposed, {"bdqval:sourceAuthority": NE_COUNTRIES}),
    BdqTest("AMENDMENT_COUNTRYCODE_FROM_COORDINATES", "8c5fe9c9-4ba9-49ef-b15a-9ccd0424e6ae", "2024-08-18",
            amend_countrycode_from_coordinates, {"bdqval:sourceAuthority": NE_COUNTRIES}),
    BdqTest("VALIDATION_DECIMALLATITUDE_NOTEMPTY", "7d2485d5-1ba7-4f25-90cb-f4480ff1a275", "2023-09-18",
            _not_empty("decimalLatitude")),
    BdqTest("VALIDATION_DECIMALLONGITUDE_NOTEMPTY", "9beb9442-d942-4f42-8b6a-fcea01ee086a", "2023-09-18",
            _not_empty("decimalLongitude")),
    BdqTest("VALIDATION_DECIMALLATITUDE_INRANGE", "b6ecda2a-ce36-437a-b515-3ae94948fe83", "2023-09-18",
            _in_range("decimalLatitude", None, None, -90, 90)),
    BdqTest("VALIDATION_DECIMALLONGITUDE_INRANGE", "0949110d-c06b-450e-9649-7c1374d940d1", "2023-09-17",
            _in_range("decimalLongitude", None, None, -180, 180)),
    BdqTest("VALIDATION_COORDINATES_NOTZERO", "1bf0e210-6792-4128-b8cc-ab6828aa4871", "2023-06-20",
            validate_coordinates_notzero),
    BdqTest("VALIDATION_MINELEVATION_INRANGE", "0bb8297d-8f8a-42d2-80c1-558f29efe798", "2023-09-17",
            _in_range("minimumElevationInMeters", "bdqval:minimumValidElevationInMeters",
                      "bdqval:maximumValidElevationInMeters"), _ELEVATION_DEFAULTS),
    BdqTest("VALIDATION_MAXELEVATION_INRANGE", "c971fe3f-84c1-4636-9f44-b1ec31fd63c7", "2023-09-18",
            _in_range("maximumElevationInMeters", "bdqval:minimumValidElevationInMeters",
                      "bdqval:maximumValidElevationInMeters"), _ELEVATION_DEFAULTS),
    BdqTest("VALIDATION_MINELEVATION_LESSTHAN_MAXELEVATION", "d708526b-6561-438e-aa1a-82cd80b06396", "2023-09-18",
            validate_minelevation_lessthan_maxelevation),
    # Taxon
    BdqTest("VALIDATION_TAXON_NOTEMPTY", "06851339-843f-4a43-8422-4e61b9a00e75", "2023-09-18",
            _any_not_empty(TAXON_TERMS, "Taxon")),
    BdqTest("VALIDATION_SCIENTIFICNAME_NOTEMPTY", "7c4b9498-a8d9-4ebb-85f1-9f200c788595", "2023-09-18",
            _not_empty("scientificName")),
    BdqTest("VALIDATION_SCIENTIFICNAMEAUTHORSHIP_NOTEMPTY", "49f1d386-5bed-43ae-bd43-deabf7df64fc", "2024-02-04",
            _not_empty("scientificNameAuthorship")),
    BdqTest("VALIDATION_SCIENTIFICNAME_FOUND", "3f335517-f442-4b98-b149-1e87ff16de45", "2023-09-17",
            validate_scientificname_found, {"bdqval:sourceAuthority": GBIF_BACKBONE}),
    BdqTest("VALIDATION_GENUS_FOUND", "f2ce7d55-5b1d-426a-b00e-6d4efe3058ec", "2023-09-17",
            validate_genus_found, {"bdqval:sourceAuthority": GBIF_BACKBONE}),
    BdqTest("VALIDATION_POLYNOMIAL_CONSISTENT", "17f03f1f-f74d-40c0-8071-2927cfc9487b", "2023-09-18",
            validate_polynomial_consistent),
    BdqTest("VALIDATION_FAMILY_FOUND", "3667556d-d8f5-454c-922b-af8af38f613c", "2023-09-17",
            validate_family_found, {"bdqval:sourceAuthority": GBIF_BACKBONE}),
    BdqTest("VALIDATION_TAXON_UNAMBIGUOUS", "4c09f127-737b-4686-82a0-7c8e30841590", "2023-09-18",
            validate_taxon_unambiguous, {"bdqval:sourceAuthority": GBIF_BACKBONE}),
    BdqTest("VALIDATION_CLASSIFICATION_CONSISTENT", "2750c040-1d4a-4149-99fe-0512785f2d5f", "2023-09-18",
            validate_classification_consistent, {"bdqval:sourceAuthority": GBIF_BACKBONE}),
    # Type status
    BdqTest("VALIDATION_TYPESTATUS_STANDARD", "4833a522-12eb-4fe0-b4cf-7f7a337a6048", "2024-11-11",
            validate_typestatus_standard, {"bdqval:sourceAuthority": TYPE_STATUS_VOCABULARY}),
    BdqTest("AMENDMENT_TYPESTATUS_STANDARDIZED", "b3471c65-b53e-453b-8282-abfa27bf1805", "2024-11-11",
            amend_typestatus_standardized, {"bdqval:sourceAuthority": TYPE_STATUS_VOCABULARY}),
    # Elevation read from the label
    BdqTest("AMENDMENT_MINELEVATIONMAXELEVATION_FROM_VERBATIM", "2d638c8b-4c62-44a0-a14d-fa147bf9823d", "2024-08-30",
            amend_elevation_from_verbatim),
]


def run_test(test: BdqTest, record: dict, params: dict | None = None, today: date | None = None) -> dict:
    """Run one test with its default Parameters, overridden by `params`."""
    merged = {**test.defaults, **{k: v for k, v in (params or {}).items() if not is_empty(v)},
              "today": today or date.today()}
    return test.run(record, merged)


# --- HerbAudit integration ----------------------------------------------------------

def prepare_record(record) -> dict:
    """Turn a HerbAudit transcription row into the record the BDQ tests expect.

    - pandas NaN and missing values become "".
    - The extraction prompt writes 0000-00-00 when the date is unknown; that carries no
      date at all, so it is treated as empty rather than as an (invalid) value.
    - HerbAudit's genus comes from the name on the label, which is what BDQ calls genericName.
    """
    record = dict(record) if record is not None else {}
    out = {k: ("" if is_empty(v) else str(v).strip()) for k, v in record.items()}
    for term in ("eventDate", "dateIdentified"):
        value = out.get(term, "")
        if re.fullmatch(r"0{4}(-00){0,2}", value):
            out[term] = ""
        else:   # older transcriptions wrote unknown parts as zeros: 1966-00-00 -> 1966, 1966-05-00 -> 1966-05
            out[term] = re.sub(r"^(\d{4})-00(-00)?$", r"\1", re.sub(r"^(\d{4}-\d{2})-00$", r"\1", value))
    if not out.get("genericName") and out.get("genus"):
        out["genericName"] = out["genus"]
    return out


def bdq_rows(filename: str, record) -> list[dict]:
    """One Excel row per BDQ test for one specimen's transcription."""
    prepared = prepare_record(record)
    rows = []
    for test in BDQ_TESTS:
        response = run_test(test, prepared)
        rows.append({"filename": filename, "test": test.label, "testGuid": test.guid, **response,
                     "value": _values_read(response.get("comment", ""), prepared)})
    return rows


def _values_read(comment: str, record: dict) -> str:
    """The transcribed values a test looked at, e.g. "eventDate=1964-12-13; country=(empty)".
    The fields come from the dwc: terms named in the test's comment."""
    terms = list(dict.fromkeys(re.findall(r"dwc:(\w+)", comment)))
    terms = [t for t in terms if t != "genericName"] or terms
    return "; ".join(f"{t}={record.get(t) or '(empty)'}" for t in terms)


def _outcome(row: dict) -> str:
    status, result = row["status"], row["result"]
    if status == RUN_HAS_RESULT:
        return _SIMPLE.get(result, "MEASURED")
    if status in (AMENDED, FILLED_IN):
        return "FIX PROPOSED"
    if status == NOT_AMENDED:
        return "NO CHANGE"
    return "CANNOT RUN"


_SIMPLE = {COMPLIANT: "PASS", NOT_COMPLIANT: "FAIL"}


def bdq_frames(rows: list[dict]):
    """Two easy-to-read tables from bdq_rows(): a specimen-by-test summary and the detail list."""
    import pandas as pd
    detail = pd.DataFrame([{
        "filename": r["filename"], "test": r["test"], "outcome": _outcome(r), "value": r["value"],
        "proposedFixOrMeasure": r["result"] if _outcome(r) in ("FIX PROPOSED", "MEASURED") else "",
        "comment": r["comment"], "status": r["status"], "testGuid": r["testGuid"],
    } for r in rows])
    if detail.empty:
        return detail, detail
    cell = detail.apply(lambda r: (r["outcome"] if r["outcome"] in ("PASS", "FAIL") else None) or (
        r["proposedFixOrMeasure"] if r["outcome"] in ("FIX PROPOSED", "MEASURED") else
        "can't run" if r["outcome"] == "CANNOT RUN" else "no change"), axis=1)
    detail["_cell"] = cell
    detail["_col"] = detail["test"].str.replace("VALIDATION_", "", regex=False)
    wide = detail.pivot(index="filename", columns="_col", values="_cell")
    order = list(dict.fromkeys(detail["_col"]))
    wide = wide[order]
    wide.insert(0, "FAIL", (wide == "FAIL").sum(axis=1))
    wide.insert(0, "PASS", (wide == "PASS").sum(axis=1))
    wide = wide.loc[list(dict.fromkeys(detail["filename"]))].reset_index()
    return detail.drop(columns=["_cell", "_col"]), wide


_BDQ_REPORT_CSS = """
:root{--bg:#fff;--fg:#1d2330;--mut:#667085;--line:#e4e7ec;--card:#f9fafb;--pass:#067647;--passbg:#d1fadf;--fail:#b42318;--failbg:#fee4e2;--grey:#475467;--greybg:#eef0f3;--fix:#175cd3;--fixbg:#dbe8fe}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171f;--fg:#e6e9f0;--mut:#98a2b3;--line:#2b3140;--card:#1b1f2a;--pass:#75e0a7;--passbg:#0b3a24;--fail:#fda29b;--failbg:#4a1713;--grey:#b9c0cc;--greybg:#2a303d;--fix:#9ec0ff;--fixbg:#16315e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0}
.sub{color:var(--mut);margin:0 0 16px}.bar{display:flex;gap:16px;align-items:center;margin:0 0 16px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 14px;overflow:hidden}
.card>summary{cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;list-style:none}
.card>summary::-webkit-details-marker{display:none}.counts{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 12px;border-top:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}td.t{font-size:12.5px;word-break:break-word}td.v{font-family:ui-monospace,monospace;font-size:12.5px;word-break:break-word}
.b{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.PASS{color:var(--pass);background:var(--passbg)}.FAIL{color:var(--fail);background:var(--failbg)}
.CANNOT{color:var(--grey);background:var(--greybg)}.FIX,.MEASURED{color:var(--fix);background:var(--fixbg)}.NOCHANGE{color:var(--grey);background:var(--greybg)}
.counts{font-weight:600}.chips{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:0 0 16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;color:var(--mut);font-size:12.5px}
.tile b{display:block;font-size:24px;color:var(--fg);line-height:1.2}.tile.fail b{color:var(--fail)}
.card>summary h2{font-size:16px}.card[open]>summary{border-bottom:1px solid var(--line)}
tr.cat td{background:var(--greybg);color:var(--grey);font-size:11.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:5px 12px}
tr:not(.cat):hover td{background:var(--bg)}
.only-fail .card tr.ok{display:none}.only-fail .card.clean{display:none}
@media (max-width:640px){th:nth-child(4),td.c{display:none}}
"""

_BDQ_BADGE = {"PASS": ("PASS", "PASS"), "FAIL": ("FAIL", "FAIL"), "CANNOT RUN": ("can't run", "CANNOT"),
              "FIX PROPOSED": ("fix", "FIX"), "MEASURED": ("measured", "MEASURED"), "NO CHANGE": ("no change", "NOCHANGE")}


_BDQ_CATEGORIES = (            # first keyword found in the test label decides; order matters
    ("Identification", ("DATEIDENTIFIED",)),
    ("Date of collection", ("EVENT",)),
    ("Type status", ("TYPESTATUS",)),
    ("Taxon and name", ("TAXON", "SCIENTIFICNAME", "GENUS", "FAMILY", "POLYNOMIAL")),
    ("Location, coordinates and elevation", ("LOCATION", "COUNTRY", "COORDINATES", "DECIMAL", "ELEVATION")),
)


def _bdq_category(label: str) -> str:
    return next((name for name, words in _BDQ_CATEGORIES if any(w in label for w in words)), "Other")


def write_bdq_html(rows: list[dict], path: str, title: str = "BDQ test results") -> None:
    """A small self-contained HTML page: for each specimen, every BDQ test with the value it
    looked at and its outcome. Tick "only failures" to hide everything else."""
    import html as _html
    esc = _html.escape
    by_specimen: dict[str, list[dict]] = {}
    for r in rows:
        by_specimen.setdefault(str(r["filename"]), []).append(r)
    def chips(outcomes: list[str]) -> str:
        shown = [("PASS", "pass", "PASS"), ("FAIL", "fail", "FAIL"), ("CANNOT RUN", "can't run", "CANNOT"),
                 ("FIX PROPOSED", "fix proposed", "FIX"), ("MEASURED", "measured", "MEASURED"),
                 ("NO CHANGE", "no change", "NOCHANGE")]
        return "".join(f'<span class="b {css}">{outcomes.count(o)} {text}</span>' for o, text, css in shown if outcomes.count(o))

    cards = []
    order = [name for name, _ in _BDQ_CATEGORIES] + ["Other"]
    for name, tests in by_specimen.items():
        tests = sorted(tests, key=lambda t: order.index(_bdq_category(t["test"])))   # stable: keeps list order inside a category
        outcomes = [_outcome(t) for t in tests]
        fails = outcomes.count("FAIL")
        body, current = [], None
        for t, outcome in zip(tests, outcomes):
            category = _bdq_category(t["test"])
            if category != current:
                current = category
                body.append(f'<tr class="cat ok"><td colspan="4">{esc(category)}</td></tr>')
            label, css = _BDQ_BADGE[outcome]
            shown = t["result"] if outcome in ("FIX PROPOSED", "MEASURED") else ""
            cls = "" if outcome == "FAIL" else ' class="ok"'
            body.append(
                f'<tr{cls}><td><span class="b {css}">{esc(label)}</span></td>'
                f'<td class="t">{esc(t["test"])}</td><td class="v">{esc(t["value"])}'
                f'{"<br>→ " + esc(shown) if shown else ""}</td><td class="c">{esc(t["comment"])}</td></tr>')
        clean = "" if fails else " clean"
        cards.append(
            f'<details class="card{clean}"{"" if not fails else " open"}><summary><h2>{esc(name)}</h2>'
            f'<span class="counts">{len(tests)} tests</span><span class="chips">{chips(outcomes)}</span></summary>'
            f'<table><tr><th>Outcome</th><th>Test</th><th>Value tested</th><th>Comment</th></tr>{"".join(body)}</table></details>')
    total_fail = sum(_outcome(r) == "FAIL" for r in rows)
    per_specimen = len(rows) // len(by_specimen) if by_specimen else 0
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{_BDQ_REPORT_CSS}</style></head><body><main><h1>{esc(title)}</h1>'
            f'<p class="sub">{len(by_specimen)} specimens · {per_specimen} tests each · {len(rows)} tests run · '
            f'{total_fail} failed. Specimens with failures are open.</p>'
            f'<div class="tiles">'
            f'<div class="tile"><b>{len(by_specimen)}</b>specimens</div><div class="tile"><b>{per_specimen}</b>tests per specimen</div>'
            f'<div class="tile"><b>{sum(_outcome(r) == "PASS" for r in rows)}</b>passed</div>'
            f'<div class="tile fail"><b>{total_fail}</b>failed</div>'
            f'<div class="tile"><b>{sum(_outcome(r) == "CANNOT RUN" for r in rows)}</b>couldn\'t run</div></div>'
            f'<div class="bar"><label><input type="checkbox" id="f"> only failures</label></div>'
            f'{"".join(cards)}</main><script>'
            f'document.getElementById("f").onchange=e=>document.body.classList.toggle("only-fail",e.target.checked)'
            f'</script></body></html>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def write_bdq_sheets(writer, rows: list[dict]) -> None:
    """Add "BDQ Summary" (one row per specimen) and "BDQ" (one row per specimen and test) to an Excel writer."""
    from openpyxl.styles import PatternFill
    detail, wide = bdq_frames(rows)
    wide.to_excel(writer, sheet_name="BDQ Summary", index=False)
    detail.to_excel(writer, sheet_name="BDQ", index=False)
    green, red, grey = (PatternFill("solid", start_color=c) for c in ("C6EFCE", "FFC7CE", "EDEDED"))
    ws = writer.sheets["BDQ Summary"]
    for row in ws.iter_rows(min_row=2, min_col=4):
        for c in row:
            c.fill = {"PASS": green, "FAIL": red, "can't run": grey}.get(c.value) or PatternFill()
    ws.freeze_panes = "B2"
    ws.column_dimensions["A"].width = 16
    for col in ws.iter_cols(min_col=2):
        ws.column_dimensions[col[0].column_letter].width = 14
        col[0].alignment = col[0].alignment.copy(wrap_text=True)
    ws = writer.sheets["BDQ"]
    ws.freeze_panes = "A2"
    for letter, w in zip("ABCDEFGH", (16, 44, 14, 50, 22, 70, 30, 12)):
        ws.column_dimensions[letter].width = w
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
        for c in row:
            c.fill = {"PASS": green, "FAIL": red, "CANNOT RUN": grey}.get(c.value) or PatternFill()
