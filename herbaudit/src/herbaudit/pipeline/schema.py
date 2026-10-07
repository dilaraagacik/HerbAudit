"""Darwin Core schema, field-alias normalization, and the single-pass extraction prompt built from it."""
from __future__ import annotations

import json


HERBARIUM_SCHEMA: dict[str, str] = {
    # Geography — all official Darwin Core terms
    "catalogNumber":             "Barcode identifier, typically a number with at least 6 digits, but fewer than 30 digits.",
    "continent":                 "Use your knowledge to infer the continent where the natural history museum specimen was originally collected.",
    "country":                   "Use your knowledge and the OCR text to infer the country where the natural history museum specimen was originally collected.",
    "stateProvince":             "The name of the sub-national administrative region than country (state, province, canton, department, region, etc.) where the specimen was originally collected.",
    "county":                    "The full, unabbreviated name of the next smaller administrative region than stateProvince (county, shire, department, parish etc.) where the specimen was originally collected.",
    "locality":                  "Description of geographic location, landmarks, regional features, nearby places, municipality, city, or any contextual information aiding in pinpointing the exact origin or location of the specimen.",
    "verbatimCoordinates":       "Verbatim location coordinates as they appear on the label. Do not convert formats. Possible coordinate types include [Latitude, Longitude, UTM, TRS].",
    "decimalLatitude":           "Latitude decimal coordinate. Correct and convert the verbatim location coordinates to conform with the decimal degrees GPS coordinate format.",
    "decimalLongitude":          "Longitude decimal coordinate. Correct and convert the verbatim location coordinates to conform with the decimal degrees GPS coordinate format.",
    "minimumElevationInMeters":  "Minimum elevation or altitude in meters. Only if units are explicit then convert from feet ('ft' or 'ft.' or 'feet') to meters ('m' or 'm.' or 'meters'). Round to integer. Values greater than 6000 are in feet and need to be converted.",
    "maximumElevationInMeters":  "Maximum elevation or altitude in meters. If only one elevation is present, then maximumElevationInMeters should remain an empty string. Only if units are explicit, then convert from feet ('ft' or 'ft.' or 'feet') to meters ('m' or 'm.' or 'meters'). Round to integer. Values greater than 6000 are in feet and need to be converted.",
    # Taxonomy — all official Darwin Core terms
    "scientificName":            "The scientific name of the taxon including genus, specific epithet, and any lower classifications. Herbarium sheets often carry a determination history — an original name later struck through/crossed out and superseded by a newer (often handwritten) annotation or determination slip. Always use the CURRENT, non-stricken determination; never a struck-through name, unless no non-stricken determination exists on the sheet at all.",
    "genus":                     "Taxonomic determination to genus, from the CURRENT (non-stricken) determination — see scientificName. Genus must be capitalized. If genus is not present use the taxonomic family name followed by the word 'indet'.",
    "specificEpithet":           "The name of the species epithet of the scientificName, from the CURRENT (non-stricken) determination — see scientificName. Only include the species epithet.",
    "scientificNameAuthorship":  "The authorship information for the scientificName, from the CURRENT (non-stricken) determination — see scientificName. Formatted according to the conventions of the applicable Darwin Core nomenclatural code.",
    "identifiedBy":              "A comma separated list of names of people, groups, or organizations who assigned the CURRENT (non-stricken) taxon determination to the subject organism — see scientificName. This is not the specimen collector.",
    "dateIdentified":            "Date that the CURRENT (non-stricken) determination was given — see scientificName. YYYY-MM-DD (zeros may be used if only partial date).",
    # Collecting — all official Darwin Core terms
    "recordedBy":                "The name of the primary collector of the specimen. Use Last Name,First Name/Initials formatting for the primary collector — this allows for easy sorting and identification. Always remove the space behind the comma separating first and last name.",
    "recordNumber":              "An identifier given to the occurrence at the time it was recorded, the specimen collector's number.",
    "verbatimEventDate":         "The verbatim original representation of the date and time information for when the specimen was collected. Date of collection exactly as it appears on the label. Do not change the format or correct typos.",
    "eventDate":                 "Date the specimen was collected formatted as year-month-day, YYYY-MM-DD. If specific components of the date are unknown, they should be replaced with zeros. Use 0000-00-00 if the entire date is unknown, YYYY-00-00 if only the year is known, and YYYY-MM-00 if year and month are known but day is not.",
    "habitat":                   "Verbatim category or description of the habitat where the specimen collection event occurred.",
}


# Aliases: old / alternate field names → canonical schema keys
_FIELD_ALIASES: dict[str, str] = {
    # collector
    "collectedBy":               "recordedBy",
    "collector":                 "recordedBy",
    "collectorName":             "recordedBy",
    "collector_name":            "recordedBy",
    # collector number
    "collectorNumber":           "recordNumber",
    "collector_number":          "recordNumber",
    # collection date
    "collectionDate":            "eventDate",
    "collection_date":           "eventDate",
    # verbatim collection date
    "verbatimCollectionDate":    "verbatimEventDate",
    "collection_date_verbatim":  "verbatimEventDate",
    # determination date
    "identifiedDate":            "dateIdentified",
    "determination_date":        "dateIdentified",
    # determiner
    "determinedBy":              "identifiedBy",
    "determination_by":          "identifiedBy",
    # name / authorship
    "taxon_name":                "scientificName",
    "taxon_author":               "scientificNameAuthorship",
    "speciesNameAuthorship":     "scientificNameAuthorship",
    # catalog
    "catalog_number":            "catalogNumber",
    # geography
    "location":                  "locality",
    "state_province":            "stateProvince",
    "county_district":           "county",
    "latitude":                  "decimalLatitude",
    "longitude":                 "decimalLongitude",
    # elevation
    "altitude":                  "minimumElevationInMeters",
    "altitude_m":                "minimumElevationInMeters",
    "elevation":                 "minimumElevationInMeters",
}


_NULL_VALUES = {
    "n/a", "na", "none", "null", "unknown", "not known", "not provided",
    "not stated", "not given", "not recorded", "not available", "not applicable",
    "no data", "no information", "–", "—", "?", "[]", "undefined",
    "not found", "not present", "absent", "missing", "unspecified",
}


def _normalize_record(record: dict) -> dict:
    """Map AI response aliases to Darwin Core keys; collapse null-like values to ''."""
    out = {}
    for k, v in record.items():
        canonical = _FIELD_ALIASES.get(k, k)
        if isinstance(v, str) and v.strip().lower() in _NULL_VALUES:
            v = ""
        out[canonical] = v
    # If scientificName is empty but genus + specificEpithet were returned, build it
    if not out.get("scientificName", "").strip():
        genus   = out.get("genus",           "").strip()
        epithet = out.get("specificEpithet", "").strip()
        if genus and epithet:
            out["scientificName"] = f"{genus} {epithet}"
        elif genus:
            out["scientificName"] = genus
    return out


def _split_record(record: dict) -> dict:
    """Nest a flat record into {"darwin_core": {...}, "herbaudit_meta": {...}}."""
    darwin_core    = {k: record.get(k, "") for k in HERBARIUM_SCHEMA}
    herbaudit_meta = {k: v for k, v in record.items() if k not in HERBARIUM_SCHEMA}
    return {"darwin_core": darwin_core, "herbaudit_meta": herbaudit_meta}


_FIELD_DESC  = "\n".join(f'  "{k}": {v}' for k, v in HERBARIUM_SCHEMA.items())
_FIELD_LIST  = ", ".join(f'"{k}"' for k in HERBARIUM_SCHEMA)
_EMPTY_JSON  = json.dumps({k: "" for k in HERBARIUM_SCHEMA}, indent=2)


_RULES_BLOCK = """\
prompt_author: Megi
prompt_author_institution: BGBM
prompt_name: BGBMvM_default
prompt_version: v-2-0
prompt_description: Prompt based on SLTPvM_defaultv2 and OSC_Symbiota (associatedCollectors). v-2-0 (2026-07-08): trimmed to official Darwin Core terms only — dropped non-DwC fields (elevationUnits, scientificNameNoAuthor, identifiedConfidence, identifiedRemarks, identificationHistory, verbatimRecordedBy, associatedCollectors, verbatimAssociatedCollectors, eventDateEnd, verbatimAccessionDate, accessionDate, accessionNumber, cultivated, specimenDescription, additionalText). verbatimCoordinates and verbatimEventDate were kept — both are official DwC terms, not custom additions.

instructions:
1. Refactor the unstructured OCR text into a dictionary based on the JSON structure outlined below.
2. Map the unstructured OCR text to the appropriate JSON key and populate the field given the user-defined rules.
3. The OCR may include helpful hints that identify handwritten text, stricken, and redacted text. Handwritten text is often a species name. The text or markdown that denotes this type of special text should not be included in the JSON, only include the actual content of the text.
4. Redacted or stricken text might have section signs on either side (§stricken text§). Handwritten text might have guillemet quotes on either side («handwritten text»).
5. If you see text in the OCR that reports on the OCR engine itself, do not include that in the JSON.
6. JSON key values are permitted to remain empty strings if the corresponding information is not found in the unstructured OCR text.
7. Duplicate dictionary fields are not allowed.
8. Ensure all JSON keys are in camel case.
9. Ensure all key-value pairs in the JSON dictionary strictly adhere to the format and data types specified in the template.
10. Ensure output JSON string is valid JSON format. It should not have trailing commas or unquoted keys.
11. Only return a JSON dictionary represented as a string. You should not explain your answer.
12. Herbarium sheets often carry a determination history: an original name later struck through/crossed out (§stricken text§) and superseded by a newer, non-stricken annotation or determination slip (often handwritten, «like this»). For scientificName, genus, specificEpithet, scientificNameAuthorship, identifiedBy, and dateIdentified specifically: always use the CURRENT, non-stricken determination. Only use a struck-through name for these fields if no non-stricken determination exists on the sheet at all.

json_formatting_instructions: Correct minor typos introduced by OCR errors.
rules:
  catalogNumber: Barcode identifier, typically a number with at least 6 digits, but fewer than 30 digits.
  scientificName: The scientific name of the taxon including genus, specific epithet, and any lower classifications.
  genus: Taxonomic determination to genus. Genus must be capitalized. If genus is not present use the taxonomic family name followed by the word 'indet'.
  specificEpithet: The name of the species epithet of the scientificName. Only include the species epithet.
  scientificNameAuthorship: The authorship information for the scientificName formatted according to the conventions of the applicable Darwin Core nomenclatural code.
  recordedBy: The name of the primary collector of the specimen. Use Last Name,First Name/Initials formatting for the primary collector, as this allows for easy sorting and identification. Always remove the space behind the comma separating first and last name.
  recordNumber: An identifier given to the occurrence at the time it was recorded, the specimen collector's number.
  identifiedBy: A comma separated list of names of people, groups, or organizations who assigned the taxon to the subject organism, the determiner. This is not the specimen collector.
  dateIdentified: Date that the most recent determination was given, in the following format. YYYY-MM-DD (zeros may be used if only partial date).
  verbatimEventDate: The verbatim original representation of the date and time information for when the specimen was collected. Date of collection exactly as it appears on the label. Do not change the format or correct typos.
  eventDate: Date the specimen was collected formatted as year-month-day, YYYY-MM-DD. If specific components of the date are unknown, they should be replaced with zeros. Use 0000-00-00 if the entire date is unknown, YYYY-00-00 if only the year is known, and YYYY-MM-00 if year and month are known but day is not.
  habitat: Verbatim category or description of the habitat where the specimen collection event occurred.
  continent: Use your knowledge to infer the continent where the natural history museum specimen was originally collected.
  country: Use your knowledge and the OCR text to infer the country where the natural history museum specimen was originally collected.
  stateProvince: The name of the sub-national administrative region than country (state, province, canton, department, region, etc.) where the specimen was originally collected.
  county: The full, unabbreviated name of the next smaller administrative region than stateProvince (county, shire, department, parish etc.) where the specimen was originally collected.
  locality: Description of geographic location, landmarks, regional features, nearby places, municipality, city, or any contextual information aiding in pinpointing the exact origin or location of the specimen.
  verbatimCoordinates: Verbatim location coordinates as they appear on the label. Do not convert formats. Possible coordinate types include [Latitude, Longitude, UTM, TRS].
  decimalLatitude: Latitude decimal coordinate. Correct and convert the verbatim location coordinates to conform with the decimal degrees GPS coordinate format.
  decimalLongitude: Longitude decimal coordinate. Correct and convert the verbatim location coordinates to conform with the decimal degrees GPS coordinate format.
  minimumElevationInMeters: Minimum elevation or altitude in meters. Only if units are explicit then convert from feet ('ft' or 'ft.' or 'feet') to meters ('m' or 'm.' or 'meters'). Round to integer. Values greater than 6000 are in feet and need to be converted.
  maximumElevationInMeters: Maximum elevation or altitude in meters. If only one elevation is present, then maximumElevationInMeters should remain an empty string. Only if units are explicit, then convert from feet ('ft' or 'ft.' or 'feet') to meters ('m' or 'm.' or 'meters'). Round to integer. Values greater than 6000 are in feet and need to be converted.

mapping:
  GEOGRAPHY: [catalogNumber, continent, country, stateProvince, county, locality, verbatimCoordinates, decimalLatitude, decimalLongitude, minimumElevationInMeters, maximumElevationInMeters]
  TAXONOMY: [scientificName, genus, specificEpithet, scientificNameAuthorship, identifiedBy, dateIdentified]
  COLLECTING: [recordedBy, recordNumber, verbatimEventDate, eventDate, habitat]
"""


# Single vision-call prompt shared by every provider (Gemini, OpenAI, Ollama).
SINGLE_PASS_PROMPT = (
    "You are an expert herbarium curator digitising natural history specimen labels.\n\n"
    "The image shows one or more herbarium labels (possibly stitched into a collage).\n"
    "Read ALL visible text carefully before filling in the JSON.\n"
    "Return ONLY a valid JSON object — do not explain your answer, no markdown fences.\n\n"
    + _RULES_BLOCK
    + "\nPlease populate the following JSON dictionary based on the label image above:\n"
    + _EMPTY_JSON
)
