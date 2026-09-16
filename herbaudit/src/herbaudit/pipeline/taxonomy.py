"""
GBIF-backbone taxonomic audit (independent of the with-reference WFO/GBIF
synonym-fallback logic in herbaudit.audit.wfo/gbif).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("herbaudit")


_TAXAUDIT_HEADERS = {
    "User-Agent": "HerbAudit/1.0 (herbarium specimen evaluation)"
}


def taxonomic_audit(name: str) -> dict[str, Any]:
    tokens   = name.strip().split()
    binomial = " ".join(tokens[:2]) if len(tokens) >= 2 else name
    audit    = {"status": "UNKNOWN", "accepted": binomial, "sources": [], "conf": 0}
    quoted   = requests.utils.quote(binomial)
    try:
        r = requests.get(
            f"https://api.gbif.org/v1/species/match?name={quoted}",
            headers=_TAXAUDIT_HEADERS, timeout=7,
        ).json()
        if r.get("matchType") not in ("NONE", "HIGHERRANK") and r.get("rank") == "SPECIES":
            audit["sources"].append("GBIF")
            audit["conf"]   = r.get("confidence", 0)
            gbif_status     = r.get("status", "")
            if gbif_status == "DOUBTFUL":
                audit["status"] = "DOUBTFUL"
            elif gbif_status == "SYNONYM":
                audit["status"] = "SYNONYM"
                acc_key = r.get("acceptedUsageKey")
                if acc_key:
                    acc = requests.get(
                        f"https://api.gbif.org/v1/species/{acc_key}",
                        headers=_TAXAUDIT_HEADERS, timeout=7,
                    ).json()
                    audit["accepted"] = acc.get("scientificName", binomial)
            else:
                audit["status"] = "ACCEPTED"
    except Exception as exc:
        log.warning("Taxonomic audit error for '%s': %s", binomial, exc)

    if not audit["sources"]:
        audit["status"] = "NOT_FOUND"
    return audit
