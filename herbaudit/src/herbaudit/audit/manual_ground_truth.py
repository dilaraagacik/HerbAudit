"""
Loading of the human-supplied annotations file: ground-truth fields (GBIF fallback
for specimens with no published occurrence) and sheet transcriptions.
"""
from __future__ import annotations

import json
import os


def load_annotation_entries(path: str | None) -> dict:
    """Read the annotations file: {specimen_id: {"transcription": str, "fields": dict}},
    both parts optional. Also accepts the older formats: a bare string is a
    transcription, and an entry with only "fields" is a ground-truth annotation.
    Returns {} if path is None or the file doesn't exist."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: ({"transcription": v} if isinstance(v, str) else v)
            for k, v in data.items() if not k.startswith("_")}


def load_manual_annotations(path: str | None) -> dict:
    """Ground-truth entries (those with a "fields" block), used when GBIF has no
    published occurrence for the specimen."""
    return {k: v for k, v in load_annotation_entries(path).items() if v.get("fields")}


def _truth_row_from_annotation(entry: dict) -> dict:
    """Same truth_row shape _process_one builds from a GBIF occurrence, built
    instead from an annotations entry's 'fields' block."""
    f = entry.get("fields", {})
    return {
        "truth_scientificName":   f.get("scientificName", "N/A"),
        "truth_genus":            f.get("genus", "N/A"),
        "truth_specificEpithet":  f.get("specificEpithet", "N/A"),
        "truth_recordedBy":       f.get("recordedBy", "N/A"),
        "truth_eventDate":        f.get("eventDate", "N/A"),
        "truth_catalogNumber":    f.get("catalogNumber", "N/A"),
        "truth_country":          f.get("country", "N/A"),
        "truth_stateProvince":    f.get("stateProvince", "N/A"),
        "truth_locality":         f.get("locality", "N/A"),
        "truth_decimalLatitude":  f.get("decimalLatitude", "N/A"),
        "truth_decimalLongitude": f.get("decimalLongitude", "N/A"),
        "truth_verbatim":         "manual annotation",
    }
