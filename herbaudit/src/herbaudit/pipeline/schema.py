"""Darwin Core field list, field-alias normalization, and the single-pass extraction prompt (prompt.yaml)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


# The fields HerbAudit extracts (all official Darwin Core terms), in the order of the JSON the model returns.
# They are fixed here because the evaluation and the BDQ tests rely on them; the wording of the
# prompt, including the rule for each field, is in prompt.yaml.
_FIELDS: tuple[str, ...] = (
    # Geography
    "catalogNumber", "continent", "country", "stateProvince",
    "county", "locality", "verbatimCoordinates", "decimalLatitude",
    "decimalLongitude", "minimumElevationInMeters", "maximumElevationInMeters", "verbatimElevation",
    # Taxonomy
    "scientificName", "genus", "specificEpithet", "scientificNameAuthorship",
    "family", "typeStatus", "identifiedBy", "dateIdentified",
    # Collecting
    "recordedBy", "recordNumber", "verbatimEventDate", "eventDate",
    "habitat",
)


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

_EMPTY_JSON = json.dumps({k: "" for k in _FIELDS}, indent=2)


# --- The extraction prompt: prompt.yaml ---------------------------------------------------
# The prompt is a YAML file so it can be edited without touching the code. This reads it, checks
# it, and writes it out as the text the model receives. The fields themselves are fixed in
# _FIELDS above, so a prompt cannot add, drop or rename a field.

class PromptError(ValueError):
    """The prompt file is missing or not valid."""


_DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompt.yaml")
_USER_PROMPT_FILE = Path.home() / ".herbaudit" / "prompt.yaml"


def _prompt_file() -> Path:
    """HERBAUDIT_PROMPT if set, else ~/.herbaudit/prompt.yaml if it exists, else the packaged prompt.yaml."""
    env = os.environ.get("HERBAUDIT_PROMPT")
    if env:
        return Path(env).expanduser()
    return _USER_PROMPT_FILE if _USER_PROMPT_FILE.is_file() else _DEFAULT_PROMPT_FILE


def _text(data: dict, key: str, path: Path, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PromptError(f"{path}: '{key}' must be text")
    return value.strip()


def load_prompt(path: Path) -> tuple[str, dict[str, str]]:
    """Read and check a prompt file. Returns the text sent to the model and the rule for each field."""
    if not path.is_file():
        raise PromptError(f"Prompt file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PromptError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise PromptError(f"{path}: expected the sections preamble, instructions, fields, mapping and closing")

    preamble, closing = _text(data, "preamble", path), _text(data, "closing", path)
    jfi = _text(data, "json_formatting_instructions", path, required=False)

    instructions = data.get("instructions")
    if not isinstance(instructions, list) or not instructions or not all(isinstance(i, str) and i.strip() for i in instructions):
        raise PromptError(f"{path}: 'instructions' must be a list of texts")

    rules = data.get("fields")
    if not isinstance(rules, dict):
        raise PromptError(f"{path}: 'fields' must list a rule for each field")
    missing = [k for k in _FIELDS if k not in rules]
    unknown = [k for k in rules if k not in _FIELDS]
    if missing or unknown:
        raise PromptError(f"{path}: 'fields' needs exactly the fields HerbAudit extracts"
                          + (f"; missing: {', '.join(missing)}" if missing else "")
                          + (f"; unknown: {', '.join(map(str, unknown))}" if unknown else ""))
    empty = [k for k, v in rules.items() if not isinstance(v, str) or not v.strip()]
    if empty:
        raise PromptError(f"{path}: 'fields' has no rule text for: {', '.join(empty)}")
    rules = {k: v.strip() for k, v in rules.items()}

    mapping = data.get("mapping")
    if not isinstance(mapping, dict) or not all(isinstance(v, list) and all(n in _FIELDS for n in v) for v in mapping.values()):
        raise PromptError(f"{path}: 'mapping' must group field names, e.g. GEOGRAPHY: [catalogNumber, country]")

    meta = data.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise PromptError(f"{path}: 'meta' must be a list of name: value pairs")

    lines = [preamble, ""]
    if meta:
        lines += [f"{k}: {str(v).strip()}" for k, v in meta.items()] + [""]
    lines += ["instructions:"] + [f"{n}. {i.strip()}" for n, i in enumerate(instructions, 1)] + [""]
    if jfi:
        lines.append(f"json_formatting_instructions: {jfi}")
    lines += ["rules:"] + [f"  {k}: {v}" for k, v in rules.items()] + ["", "mapping:"]
    lines += [f"  {group}: [{', '.join(names)}]" for group, names in mapping.items()]
    lines += ["", closing, _EMPTY_JSON]
    return "\n".join(lines), rules


# Single vision-call prompt shared by every provider (Gemini, OpenAI, Ollama).
PROMPT_SOURCE = _prompt_file()
SINGLE_PASS_PROMPT, _RULES = load_prompt(PROMPT_SOURCE)
HERBARIUM_SCHEMA: dict[str, str] = {k: _RULES[k] for k in _FIELDS}     # field -> its rule, in _FIELDS order
