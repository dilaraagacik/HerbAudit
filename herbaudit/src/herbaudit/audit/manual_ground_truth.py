"""
Manual (human-supplied) ground-truth annotation loading — GBIF fallback for
specimens with no published GBIF occurrence.
"""
from __future__ import annotations

import json
import os


def load_manual_annotations(path: str | None) -> dict:
    """
    Human-supplied ground-truth entries (see manual_annotations.json), keyed
    by catalog number. run_audit's per-specimen accuracy scoring falls back
    to one of these whenever fetch_gbif_data() finds no published GBIF
    occurrence for that specimen. Returns {} if path is None or the file
    doesn't exist.
    """
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _truth_row_from_annotation(entry: dict) -> dict:
    """Same truth_row shape _process_one builds from a GBIF occurrence, built
    instead from a manual_annotations.json entry's 'fields' block."""
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
