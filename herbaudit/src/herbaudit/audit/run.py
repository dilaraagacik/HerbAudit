"""
Orchestration entry point: run_audit() (with-reference) and
_run_no_reference() (--no_reference mode).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .text import strip_authors
from .gbif import fetch_gbif_data, smart_get, _gbif_fuzzy_resolve
from .wfo import _load_wfo_cache, _save_wfo_cache, _wfo_lookup, _wfo_checklistbank_match
from .report import build_card, _make_unmatched_card, build_no_reference_card, _NOREF_FIELDS, fmt_pct
from .manual_ground_truth import load_manual_annotations, _truth_row_from_annotation

def _lazy_imports():
    """Import slow packages on first use, not at CLI startup."""
    global cv2, np, pd, genai, genai_types, HerbAudit, taxonomic_audit
    import cv2
    import numpy as np
    import pandas as pd
    from google import genai
    from google.genai import types as genai_types
    from herbaudit.pipeline.herb_audit import HerbAudit
    from herbaudit.pipeline.taxonomy import taxonomic_audit
    # Also trigger the other modules' own _lazy_imports() so this single call
    # populates everything downstream code needs.
    from . import text as _text_mod, dates as _dates_mod, scoring as _scoring_mod
    _text_mod._lazy_imports()
    _dates_mod._lazy_imports()
    _scoring_mod._lazy_imports()

cv2 = np = pd = genai = genai_types = HerbAudit = taxonomic_audit = None


OUTPUT_HTML       = "results.html"
OUTPUT_XLSX       = "results.xlsx"

# Darwin Core terms, in the column order of the no-reference Excel export.
DWC_EXCEL_FIELDS = [
    "catalogNumber", "scientificName", "scientificNameAuthorship", "genus", "specificEpithet",
    "identifiedBy", "dateIdentified", "recordedBy", "recordNumber", "eventDate", "verbatimEventDate",
    "continent", "country", "stateProvince", "county", "locality", "verbatimCoordinates",
    "decimalLatitude", "decimalLongitude", "minimumElevationInMeters", "maximumElevationInMeters",
    "habitat",
]


# Evaluation-mode field names that are not Darwin Core terms -> the DwC term used in the Excel export.
_EXCEL_FIELD_NAME = {"Generic Name": "genus"}


def _taxonomic_check(sci_name: str):
    """(taxonomicStatus, acceptedNameUsage, nameAccordingTo) for one name:
    WFO Checklistbank first, GBIF as fallback — same logic as the no-reference report."""
    sci_name = sci_name.strip()
    if not sci_name or sci_name.lower() in ("nan", "none", "n/a"):
        return "", "", ""
    try:
        clean = strip_authors(sci_name)
        cb = _wfo_checklistbank_match(sci_name)
        if cb:
            cb_status = (cb.get("status") or "").lower()
            if cb_status in ("accepted", "provisionally accepted"):
                return "accepted", clean, "WFO"
            if "synonym" in cb_status:
                cb_accepted = strip_authors(cb.get("accepted") or "")
                if cb_accepted:
                    return "synonym", cb_accepted, "WFO"
        gbif = taxonomic_audit(clean)
        status = (gbif.get("status") or "").upper()
        if status == "ACCEPTED":
            return "accepted", gbif.get("accepted") or clean, "GBIF"
        if status == "SYNONYM":
            return "synonym", gbif.get("accepted") or "", "GBIF"
    except Exception as exc:
        print(f"  taxonomy check failed for '{sci_name}': {exc}")
    return "not found", "", ""


def _taxonomic_status(tax_info):
    """(taxonomicStatus, acceptedNameUsage, nameAccordingTo) from the taxonomy check."""
    if not tax_info:
        return "", "", ""
    if tax_info.get("accepted"):  # confirmed by WFO Checklistbank
        status = "synonym" if tax_info.get("is_synonym") else "accepted"
        return status, tax_info["accepted"], tax_info.get("tax_source") or "WFO"
    gbif = (tax_info.get("gbif_status") or "").upper()  # GBIF fallback
    if gbif == "ACCEPTED":
        return "accepted", tax_info.get("gbif_accepted") or "", "GBIF"
    if gbif == "SYNONYM":
        return "synonym", tax_info.get("gbif_accepted") or "", "GBIF"
    return "not found", "", ""


# Anchored to __file__ (not a relative path) so it resolves correctly
# regardless of the cwd `herbaudit` is invoked from.
DEFAULT_ANNOTATIONS_PATH = str(Path(__file__).parent / "files" / "annotations.json")


def _run_no_reference(vv_df: "pd.DataFrame", gemini_model: str, images_folder=None, output_tag=None):
    """
    Build a standalone HTML report with no GBIF lookup.
    Shows all extracted fields, WFO taxonomy status, cost and resolution.
    Interactive JS resolution simulator recalculates token cost live.
    """
    OUTPUT_NOREF      = f"results_noreference_{output_tag}.html" if output_tag else "results_noreference.html"
    OUTPUT_NOREF_XLSX = f"results_noreference_{output_tag}.xlsx" if output_tag else "results_noreference.xlsx"

    cards_html  = ""
    total_cost  = 0.0
    n_synonyms  = 0
    excel_rows  = []

    print(f"Building noreference report for {len(vv_df)} specimens...")

    for _, ai_row in vv_df.iterrows():
        fname   = str(ai_row.get("filename", "")).strip()
        pure_id = re.sub(r"\.(jpg|jpeg|png|JPG|PNG)$", "", fname, flags=re.IGNORECASE).strip()

        # WFO accepted/synonym status via GBIF Checklistbank (has a real
        # taxonomicStatus field, unlike matching_rest.php).
        sci_name = str(ai_row.get("scientificName", "")).strip()
        tax_info = None
        if sci_name and sci_name.lower() not in ("nan", "none", "n/a", ""):
            clean_sci      = strip_authors(sci_name)
            sci_authorship = str(ai_row.get("scientificNameAuthorship", "")).strip()
            synonyms = frozenset()

            cb_info = _wfo_checklistbank_match(sci_name)
            accepted, is_syn, tax_source = None, False, None
            if cb_info:
                cb_status = (cb_info.get("status") or "").lower()
                if cb_status in ("accepted", "provisionally accepted"):
                    accepted, tax_source = clean_sci, "WFO"
                elif "synonym" in cb_status:
                    cb_accepted = strip_authors(cb_info.get("accepted") or "")
                    if cb_accepted:
                        accepted, is_syn, tax_source = cb_accepted, True, "WFO"

            if is_syn:
                n_synonyms += 1

            # WFO existence + hierarchy check (matching_rest.php) — a second,
            # independent signal shown as its own badge, not folded into the
            # Checklistbank accepted/synonym claim above.
            wfo_found, wfo_hierarchy, wfo_closest, wfo_closest_sim = _wfo_lookup(sci_name, sci_authorship)

            # GBIF is only a fallback when Checklistbank didn't confirm status
            if accepted is None:
                gbif_tax = taxonomic_audit(clean_sci)
                gbif_status   = gbif_tax.get("status", "NOT_FOUND")
                gbif_accepted = gbif_tax.get("accepted", "")
                wfo_note   = "  (found in WFO)" if wfo_found else ""
                print(f"  {pure_id} → WFO: not found  GBIF fallback: {gbif_status}{wfo_note}")
            else:
                gbif_status   = None
                gbif_accepted = ""
                print(f"  {pure_id} → {tax_source}: {'synonym of ' + accepted if is_syn else 'accepted'}")

            gbif_fuzzy_matched, gbif_fuzzy_accepted, gbif_fuzzy_sim = _gbif_fuzzy_resolve(sci_name)
            tax_info = {
                "source_name":   clean_sci,
                "tax_source":    tax_source,
                "accepted":      accepted,
                "is_synonym":    is_syn,
                "synonyms":      synonyms,
                "gbif_status":   gbif_status,
                "gbif_accepted": gbif_accepted,
                "gbif_fuzzy_matched":   gbif_fuzzy_matched,
                "gbif_fuzzy_accepted":  gbif_fuzzy_accepted,
                "gbif_fuzzy_sim":       gbif_fuzzy_sim,
                "wfo_found":        wfo_found,
                "wfo_hierarchy":    wfo_hierarchy,
                "wfo_closest":      wfo_closest,
                "wfo_closest_sim":  wfo_closest_sim,
            }

        # Image metadata (populated when coming from HerbAudit image mode)
        img_meta = {
            "width":        ai_row.get("_img_width"),
            "height":       ai_row.get("_img_height"),
            "cost_usd":     ai_row.get("_cost_usd"),
            "input_tokens": ai_row.get("_input_tokens"),
            "output_tokens":ai_row.get("_output_tokens"),
            "model":        ai_row.get("_model", gemini_model),
        }
        cost, card_html = build_no_reference_card(
            pure_id, ai_row, tax_info, img_meta, images_folder
        )
        total_cost  += cost
        cards_html  += card_html

        # Columns use Darwin Core term names, in DWC_EXCEL_FIELDS order.
        excel_row = {"filename": pure_id}
        for term in DWC_EXCEL_FIELDS:
            excel_row[term] = str(ai_row.get(term, "")).strip()   # transcription as extracted
        # Synonym / accepted-name check (Darwin Core taxonomicStatus, acceptedNameUsage)
        status, accepted_name, according_to = _taxonomic_status(tax_info)
        excel_row["taxonomicStatus"]    = status
        excel_row["acceptedNameUsage"]  = accepted_name
        excel_row["nameAccordingTo"]    = according_to
        if tax_info:
            excel_row["gbifFuzzySuggestion"] = tax_info.get("gbif_fuzzy_accepted")
            excel_row["gbifFuzzySimilarity"] = tax_info.get("gbif_fuzzy_sim")
        excel_row["model"]        = img_meta.get("model")
        excel_row["inputTokens"]  = img_meta.get("input_tokens")
        excel_row["outputTokens"] = img_meta.get("output_tokens")
        excel_row["costUSD"]      = img_meta.get("cost_usd")
        excel_rows.append(excel_row)

    n           = len(vv_df)
    model_label = vv_df["_model"].iloc[0] if "_model" in vv_df.columns and len(vv_df) else gemini_model

    _key_fields = ("scientificName", "recordedBy", "eventDate", "country",
                   "stateProvince", "locality", "catalogNumber")
    n_failed = sum(
        1 for _, row in vv_df.iterrows()
        if not any(
            str(row.get(f, "")).strip()
            for f in _key_fields
            if str(row.get(f, "")).strip().lower() not in ("", "nan", "none", "n/a")
        )
    )
    failed_stat_box = (
        f"<div class='stat-box'><div class='stat-val' style='color:#fca5a5;'>{n_failed}</div>"
        f"<div class='stat-label'>Failed extractions</div></div>"
    ) if n_failed else ""

    html = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Herbarium Audit</title>
  <link rel='preconnect' href='https://fonts.googleapis.com'>
  <link href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600&display=swap' rel='stylesheet'>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f5f5f7; font-family: 'DM Sans', sans-serif; color: #1f2937; }}
    .topbar {{
      background: linear-gradient(90deg, #1e3a5f 0%, #1e40af 100%);
      color: #fff; padding: 0 48px; display: flex; align-items: center;
      justify-content: space-between; height: 60px; position: sticky;
      top: 0; z-index: 100; box-shadow: 0 2px 12px rgba(30,64,175,0.3);
    }}
    .topbar-title {{ font-family: 'Syne', sans-serif; font-size: 1rem; font-weight: 800; }}
    .topbar-cost  {{ font-family: 'IBM Plex Mono', monospace; font-size: 1rem; font-weight: 600; color: #bfdbfe; }}
    .hero {{
      background: linear-gradient(135deg, #1e3a5f 0%, #1d4ed8 60%, #1e40af 100%);
      padding: 48px 48px 56px; position: relative; overflow: hidden;
    }}
    .hero h1 {{ font-family: 'Syne', sans-serif; font-size: 2.6rem; font-weight: 800; color: #fff; margin: 0 0 4px; }}
    .hero-sub {{ color: #bfdbfe; font-size: 0.9rem; margin-bottom: 36px; }}
    .stats-row {{ display: flex; flex-wrap: wrap; gap: 16px; max-width: 1000px; }}
    .stat-box {{
      background: rgba(0,0,0,0.18); border: 1px solid rgba(255,255,255,0.12);
      border-radius: 12px; padding: 16px 20px; min-width: 140px;
    }}
    .stat-val   {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.6rem; font-weight: 600; line-height: 1; margin-bottom: 4px; }}
    .stat-label {{ font-size: 0.65rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: rgba(255,255,255,0.55); }}
    .controls {{
      padding: 16px 48px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
      background: #fff; border-bottom: 1px solid #e5e7eb; position: sticky; top: 60px; z-index: 99;
    }}
    .controls-label {{ font-size: 0.75rem; font-weight: 700; color: #6b7280; }}
    .sort-btn {{
      padding: 6px 16px; border-radius: 99px; border: 1.5px solid #e5e7eb;
      background: #fff; font-size: 0.75rem; font-weight: 700; color: #6b7280; cursor: pointer;
    }}
    .sort-btn.active {{ background: #1d4ed8; border-color: #1d4ed8; color: #fff; }}
    #cardContainer {{ max-width: 1180px; margin: 32px auto; padding: 0 32px 64px; }}
    .audit-card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.10), 0 16px 40px rgba(0,0,0,.09) !important; }}
  </style>
</head>
<body>

<div class='topbar'>
  <span class='topbar-title'>Herbarium Audit</span>
  <span class='topbar-cost' id='topbarCost'>Total cost: ${total_cost:.4f}</span>
</div>

<div class='hero'>
  <h1>Herbarium Audit</h1>
  <p class='hero-sub'>AI label extraction · WFO / GBIF taxonomy check · GBIF fuzzy name matching · no occurrence reference</p>
  <div class='stats-row'>
    <div class='stat-box'><div class='stat-val' style='color:#e0e7ff;'>{n}</div><div class='stat-label'>Specimens</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#fde68a;'>${total_cost:.4f}</div><div class='stat-label'>Total cost</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#a5f3fc;'>${(total_cost/n if n else 0):.5f}</div><div class='stat-label'>Avg cost / specimen</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#d8b4fe;'>{n_synonyms}</div><div class='stat-label'>Synonyms found</div></div>
    {failed_stat_box}
    <div class='stat-box'><div class='stat-val' style='color:#bfdbfe; font-size:1rem;'>{model_label}</div><div class='stat-label'>Model</div></div>
  </div>
</div>

<div class='controls'>
  <span class='controls-label'>Sort:</span>
  <button class='sort-btn active' id='srtName' onclick='sortCards("name")'>A → Z</button>
</div>

<div id='cardContainer'>
  {cards_html}
</div>

<script>
function sortCards(mode) {{
  const container = document.getElementById('cardContainer');
  const cards = Array.from(container.querySelectorAll('.audit-card'));
  cards.sort((a, b) => {{
    const ta = (a.querySelector('h3') || {{}}).textContent || '';
    const tb = (b.querySelector('h3') || {{}}).textContent || '';
    return ta.localeCompare(tb);
  }});
  cards.forEach(c => container.appendChild(c));
  document.getElementById('srtName')?.classList.add('active');
}}
sortCards('name');
</script>
</body>
</html>"""

    with open(OUTPUT_NOREF, "w", encoding="utf-8") as f:
        f.write(html)

    pd.DataFrame(excel_rows).to_excel(OUTPUT_NOREF_XLSX, index=False)

    print(f"\nNo reference report → {OUTPUT_NOREF}")
    print(f"   Excel export → {OUTPUT_NOREF_XLSX}")
    print(f"   Specimens  : {n}")
    print(f"   Synonyms   : {n_synonyms}")
    print(f"   Total cost : ${total_cost:.4f}")
    if n:
        print(f"   Avg cost   : ${total_cost/n:.5f}")


def run_audit(input_path=None, images_folder=None, truth_path=None,
              gemini_api_key=None, gemini_model="gemini-3.5-flash-lite",
              openai_api_key=None, openai_model="gpt-4o-mini",
              ollama_model=None, ollama_host="http://localhost:11434",
              media_resolution="high",
              weights=None,
              noreference=False, max_resolution=None,
              use_collage=True, detector="auto",
              output_tag=None, force=False,
              batch=False, batch_chunk_mb=1000, batch_submit_workers=5,
              batch_poll_interval=30.0,
              annotations_path=None):
    """
    Run the full GBIF-comparison audit (or the --no_reference report when
    `noreference` is set) and write the HTML/Excel results.

    output_tag: appended to output filenames so separate runs don't overwrite
    each other.
    annotations_path: JSON with per-specimen ground-truth "fields" (used when GBIF
    has no matching occurrence) and label "transcription" text (used to settle
    AI/GBIF disagreements); defaults to the packaged file, pass "" to disable.
    """
    if annotations_path is None:
        annotations_path = DEFAULT_ANNOTATIONS_PATH
    _lazy_imports()
    if not input_path:
        raise ValueError("Provide an input_path (CSV, Excel, or image folder).")

    inp = Path(input_path)

    # A single image file should also be treated as image mode, or it falls
    # through to the CSV/Excel branch and pd.read_csv() chokes on the bytes.
    from herbaudit.pipeline.images import IMAGE_EXTENSIONS
    _is_single_image = inp.is_file() and inp.suffix.lower() in IMAGE_EXTENSIONS

    # Default for CSV/Excel mode; image mode below overrides with a model-specific name.
    output_dir = "./herbaudit_output"

    if inp.is_dir() or _is_single_image:
        if not gemini_api_key and not openai_api_key and not ollama_model:
            raise ValueError(
                "input_path is an image file or folder — provide --gemini-key, --openai-key, or --ollama-model."
            )
        provider = "Gemini" if gemini_api_key else "OpenAI" if openai_api_key else "Ollama"
        what = "image folder" if inp.is_dir() else "image file"
        print(f"{what.capitalize()} detected — running {provider} extraction via HerbAudit...")

        ha = HerbAudit(
            gemini_api_key   = gemini_api_key,
            gemini_model     = gemini_model,
            openai_api_key   = openai_api_key,
            openai_model     = openai_model,
            ollama_model     = ollama_model,
            ollama_host      = ollama_host,
            media_resolution = media_resolution,
        )

        # One output folder per model so a rerun with a different --model
        # doesn't overwrite the previous model's cache/report.
        safe_model = re.sub(r"[^A-Za-z0-9_.-]", "_", ha.model)
        output_dir = f"./herbaudit_output_{safe_model}"

        ha.run_full_audit(
            input_path     = str(inp),
            output_dir     = output_dir,
            skip_existing  = not force,
            skip_taxonomy  = noreference,
            max_resolution = max_resolution,
            weights        = weights,
            use_collage    = use_collage,
            detector       = detector,
            batch                = batch,
            batch_chunk_mb       = batch_chunk_mb,
            batch_submit_workers = batch_submit_workers,
            batch_poll_interval  = batch_poll_interval,
        )

        rows = []
        for r in ha.results:
            if "error" in r:
                continue
            # Records are nested as {"darwin_core": {...}, "herbaudit_meta": {...}};
            # fall back to the record itself for older flat *_audit.json files.
            dc   = r.get("darwin_core", r)
            meta = r.get("herbaudit_meta", r)
            row = {}
            from herbaudit.pipeline.schema import HERBARIUM_SCHEMA
            for field in HERBARIUM_SCHEMA:
                row[field] = dc.get(field, "")
            row["filename"]      = re.sub(r"^_resized_", "", str(meta.get("source_image", "")))
            row["gbifID"]        = dc.get("catalogNumber", "")
            row["_cost_usd"]     = meta.get("cost_usd")
            row["_input_tokens"] = meta.get("tokens_in")
            row["_output_tokens"]= meta.get("tokens_out")
            row["_model"]        = meta.get("model", ha.model)
            row["_img_width"]    = meta.get("image_width")
            row["_img_height"]   = meta.get("image_height")
            rows.append(row)

        if not rows:
            raise ValueError("HerbAudit returned no usable records.")

        vv_df = pd.DataFrame(rows)
        print(f"  → {len(vv_df)} specimens extracted, proceeding to scoring...")

    else:
        if inp.suffix.lower() in (".xlsx", ".xls"):
            raw_df = pd.read_excel(input_path)
        else:
            raw_df = pd.read_csv(input_path, sep=None, engine="python")
        vv_df = raw_df.copy()

    # Normalize VoucherVision column names to the Darwin Core names used throughout.
    _vc_renames = {
        "collectedBy":    "recordedBy",
        "collectionDate": "eventDate",
    }
    vv_df.rename(
        columns={k: v for k, v in _vc_renames.items()
                 if k in vv_df.columns and v not in vv_df.columns},
        inplace=True,
    )

    def robust_id_clean(series):
        return (
            series.astype(str)
                  .str.replace(r"\.0$", "", regex=True)
                  .str.strip()
                  .replace(["nan", "None", "<NA>"], "")
        )

    vv_df["gbifID_clean"] = robust_id_clean(vv_df["gbifID"]) if "gbifID" in vv_df.columns else ""

    # Load persisted WFO cache so previously seen names cost 0 HTTP requests
    _load_wfo_cache()

    # Fall back to the input's own folder so card thumbnails can be found
    # locally (a single file's parent, since get_img() needs a directory).
    images_folder = images_folder or (
        str(inp) if inp.is_dir() else str(inp.parent) if inp.is_file() else None
    )

    if noreference:
        _run_no_reference(vv_df, gemini_model, images_folder, output_tag=output_tag)
        _save_wfo_cache()
        return

    # Manual ground-truth fallback for specimens fetch_gbif_data() can't find.
    # {} if annotations_path is None/missing.
    manual_annotations = load_manual_annotations(annotations_path)
    if manual_annotations:
        print(f"  Manual annotations loaded: {len(manual_annotations)} "
              f"entr{'y' if len(manual_annotations) == 1 else 'ies'} from {annotations_path}")

    from herbaudit.manual_date_reconcile import load_manual_transcriptions
    manual_transcriptions = load_manual_transcriptions(annotations_path)
    if manual_transcriptions:
        print(f"  Manual transcriptions loaded: {len(manual_transcriptions)} "
              f"specimen(s) from {annotations_path}")

    field_map = {
        "Taxonomy": {
            "scientificName":  (True,  False, False, False),
            "genus":           (True,  False, False, False),
            "specificEpithet": (True,  False, False, False),
        },
        "Collector": {
            "recordedBy":  (False, True,  False, False),
            "eventDate":   (False, False, True,  False),
        },
        "Geography": {
            "country":         (False, False, False, True),
            "stateProvince":   (False, False, False, True),
            "locality":        (False, False, False, True),
        },
    }

    def _persist_gbif_accuracy(pure_id: str, accuracy: float | None) -> None:
        """
        Patch the herbaudit_accuracy field into this specimen's already-written
        <output_dir>/<pure_id>_audit.json. No-op if that file isn't there.
        """
        json_path = Path(output_dir) / f"{pure_id}_audit.json"
        if not json_path.exists():
            return
        with open(json_path, encoding="utf-8") as f:
            record = json.load(f)
        value = round(accuracy, 4) if accuracy is not None else None
        if "herbaudit_meta" in record:
            record["herbaudit_meta"]["herbaudit_accuracy"] = value
        else:
            record["herbaudit_accuracy"] = value  # older flat *_audit.json file
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    def _process_one(ai_row):
        fname   = str(ai_row.get("filename", ""))
        pure_id = re.sub(r"\.(jpg|jpeg|png|JPG|PNG)$", "", fname, flags=re.IGNORECASE).strip()
        gid     = pure_id
        manual_text = manual_transcriptions.get(pure_id)

        occ, frag, verb = fetch_gbif_data(pure_id)

        if not occ or not isinstance(occ, dict):
            annotation = manual_annotations.get(pure_id)
            if annotation is None:
                print(f"  {pure_id}: no GBIF data found")
                _persist_gbif_accuracy(pure_id, None)
                return "unmatched", (-1.0, _make_unmatched_card(pure_id, gid), False, [], pure_id, gid)

            verified_str = "verified" if annotation.get("verified") else "UNVERIFIED draft"
            print(f"  {pure_id}: no GBIF data — scoring against manual annotation ({verified_str})")
            truth_row = _truth_row_from_annotation(annotation)
            avg, card_html, has_image, field_results = build_card(
                pure_id, gid, truth_row, ai_row, field_map, images_folder=images_folder,
                manual_text=manual_text, strict_geo=True
            )
            print(f"  MATCHED {pure_id}  ({fmt_pct(avg)}, vs. manual annotation)")
            _persist_gbif_accuracy(pure_id, avg)
            return "matched", (avg, card_html, has_image, field_results, pure_id, gid)

        gid = str(occ.get("gbifId", pure_id))
        event_date = (
            occ.get("eventDate")
            or occ.get("verbatimEventDate")
            or "-".join(str(occ.get(k, "")) for k in ("year", "month", "day") if occ.get(k))
            or "N/A"
        )
        # verbatimLocality (as-submitted text) is closer to the label than
        # GBIF's cleaned "locality", so prefer it as ground truth; fall back
        # to "locality" only when no verbatim form exists.
        loc_verbatim    = smart_get("verbatimLocality", frag, verb, occ)
        loc_interpreted = smart_get("locality",         frag, verb, occ)
        truth_locality = loc_verbatim if loc_verbatim != "N/A" else loc_interpreted
        truth_row = {
            "truth_scientificName":  occ.get("scientificName", "N/A"),
            "truth_genus":           smart_get("genus",           frag, verb, occ),
            "truth_specificEpithet": smart_get("specificEpithet", frag, verb, occ),
            "truth_recordedBy":      occ.get("recordedBy",    "N/A"),
            "truth_eventDate":       event_date,
            "truth_catalogNumber":   occ.get("catalogNumber", "N/A"),
            "truth_country":         occ.get("country",       "N/A"),
            "truth_stateProvince":   occ.get("stateProvince", "N/A"),
            "truth_locality":        truth_locality,
            "truth_decimalLatitude": occ.get("decimalLatitude",  "N/A"),
            "truth_decimalLongitude":occ.get("decimalLongitude", "N/A"),
            "truth_verbatim":        str(verb),
        }
        avg, card_html, has_image, field_results = build_card(
            pure_id, gid, truth_row, ai_row, field_map, images_folder=images_folder,
            manual_text=manual_text
        )
        print(f"  MATCHED {pure_id}  ({fmt_pct(avg)})")
        _persist_gbif_accuracy(pure_id, avg)
        return "matched", (avg, card_html, has_image, field_results, pure_id, gid)

    # GBIF allows concurrent requests; 6 workers gives ~4-6x speedup over serial.
    matched_cards   = []
    unmatched_cards = []

    rows = list(vv_df.iterrows())
    print(f"Auditing {len(rows)} specimens (parallel, 6 workers)...")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_process_one, row): row for _, row in rows}
        for fut in as_completed(futures):
            try:
                kind, result = fut.result()
            except Exception as exc:
                fname = str(futures[fut].get("filename", "?"))
                pure_id = re.sub(r"\.(jpg|jpeg|png|JPG|PNG)$", "", fname, flags=re.IGNORECASE).strip()
                print(f"  {pure_id}: worker crashed ({exc}) — treating as unmatched")
                unmatched_cards.append(
                    (-1.0, _make_unmatched_card(pure_id, pure_id), False, [], pure_id, pure_id)
                )
                continue
            if kind == "matched":
                matched_cards.append(result)
            else:
                unmatched_cards.append(result)

    matched_cards.sort(key=lambda x: x[0], reverse=True)
    _save_wfo_cache()   # persist any newly resolved names
    all_cards  = matched_cards + unmatched_cards
    cards_html = "\n".join(c for _, c, _, _, _, _ in all_cards)

    scored      = [s for s, _, _, _, _, _ in matched_cards]
    overall     = sum(scored) / len(scored) if scored else 0.0
    n           = len(matched_cards)
    n_unmatched = len(unmatched_cards)
    n_images    = n + n_unmatched
    n_good      = sum(1 for s in scored if s >= 0.85)
    n_ok        = sum(1 for s in scored if 0.60 <= s < 0.85)
    n_poor      = sum(1 for s in scored if s < 0.60)

    images_stat_box = f"""
    <div class='stat-box'>
      <div class='stat-val' style='color:#93c5fd;'>{n_images}</div>
      <div class='stat-label'>Images evaluated</div>
    </div>"""

    unmatched_stat_box = (
        f"<div class='stat-box'><div class='stat-val' style='color:#fca5a5;'>{n_unmatched}</div>"
        f"<div class='stat-label'>No truth found</div></div>"
    ) if n_unmatched else ""

    # Mean score for each field across every matched specimen (excluding
    # rows that weren't scored), so accuracy can be judged per-field.
    field_order = []
    for fields in field_map.values():
        for vv_col in fields:
            display_col = "Generic Name" if vv_col == "genus" else vv_col
            if display_col not in field_order:
                field_order.append(display_col)
    field_order.append("coordinates")

    field_scores_all: dict[str, list[float]] = {f: [] for f in field_order}
    for _, _, _, field_results, _, _ in matched_cards:
        for fr in field_results:
            if fr["score"] is not None:
                field_scores_all.setdefault(fr["field"], []).append(fr["score"])

    field_accuracy = []
    for f in field_order:
        scores = field_scores_all.get(f, [])
        mean_acc = sum(scores) / len(scores) if scores else None
        field_accuracy.append({"field": f, "mean_accuracy": mean_acc, "n_scored": len(scores)})

    def _field_acc_row(fa):
        if fa["mean_accuracy"] is None:
            pct_label, color, width = "—", "rgba(255,255,255,0.35)", 0
        else:
            pct_label = fmt_pct(fa['mean_accuracy'])
            color = "#86efac" if fa["mean_accuracy"] >= 0.85 else ("#fde68a" if fa["mean_accuracy"] >= 0.60 else "#fca5a5")
            width = round(fa["mean_accuracy"] * 100, 1)
        return f"""
        <div class='field-row'>
          <span class='field-name'>{fa['field']}</span>
          <div class='field-bar-track'><div class='field-bar-fill' style='width:{width}%; background:{color};'></div></div>
          <span class='field-pct' style='color:{color};'>{pct_label}</span>
          <span class='field-n'>n={fa['n_scored']}</span>
        </div>"""

    field_accuracy_html = "\n".join(_field_acc_row(fa) for fa in field_accuracy)


    # 7. WRITE HTML
    html = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>AI Evaluation - Herbarium Audit</title>
  <link rel='preconnect' href='https://fonts.googleapis.com'>
  <link href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600&display=swap' rel='stylesheet'>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 0; background: #f5f5f7; font-family: 'DM Sans', sans-serif; color: #1f2937; }}
    .topbar {{
      background: linear-gradient(90deg, #14532d 0%, #166534 100%);
      color: #fff; padding: 0 48px; display: flex; align-items: center;
      justify-content: space-between; height: 60px; position: sticky;
      top: 0; z-index: 100; box-shadow: 0 2px 12px rgba(20,83,45,0.25);
    }}
    .topbar-title {{ font-family: 'Syne', sans-serif; font-size: 1rem; font-weight: 800; letter-spacing: 0.5px; }}
    .topbar-score {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.1rem; font-weight: 600; color: #bbf7d0; }}
    .hero {{
      background: linear-gradient(135deg, #14532d 0%, #15803d 60%, #166534 100%);
      padding: 48px 48px 56px; border-bottom: 1px solid #166534; position: relative; overflow: hidden;
    }}
    .hero::before {{
      content: ''; position: absolute; top: -60px; right: -60px;
      width: 300px; height: 300px; border-radius: 50%;
      background: rgba(255,255,255,0.04); pointer-events: none;
    }}
    .hero h1 {{ font-family: 'Syne', sans-serif; font-size: 2.8rem; font-weight: 800; color: #fff; margin: 0 0 4px; line-height: 1.1; }}
    .hero-sub {{ color: #bbf7d0; font-size: 0.9rem; margin-bottom: 36px; }}
    .stats-row {{ display: flex; flex-wrap: wrap; gap: 16px; max-width: 1180px; }}
    .stat-box {{
      background: rgba(0,0,0,0.18); border: 1px solid rgba(255,255,255,0.12);
      border-radius: 12px; padding: 16px 20px; backdrop-filter: blur(4px); min-width: 120px;
    }}
    .stat-val {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.6rem; font-weight: 600; line-height: 1; margin-bottom: 4px; }}
    .stat-label {{ font-size: 0.65rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: rgba(255,255,255,0.55); }}
    .field-chart {{ display: flex; flex-direction: column; gap: 9px; max-width: 720px; }}
    .field-row {{ display: flex; align-items: center; gap: 12px; }}
    .field-name {{
      width: 150px; flex-shrink: 0; font-size: 0.72rem; font-weight: 600;
      color: rgba(255,255,255,0.85); text-align: right; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis;
    }}
    .field-bar-track {{ flex: 1; height: 12px; background: rgba(0,0,0,0.22); border-radius: 6px; overflow: hidden; }}
    .field-bar-fill {{ height: 100%; border-radius: 6px; }}
    .field-pct {{
      width: 42px; flex-shrink: 0; font-family: 'IBM Plex Mono', monospace;
      font-size: 0.75rem; font-weight: 600; text-align: right;
    }}
    .field-n {{ width: 46px; flex-shrink: 0; font-size: 0.62rem; color: rgba(255,255,255,0.45); }}
    .controls {{
      padding: 20px 48px; display: flex; align-items: center; gap: 12px;
      background: #fff; border-bottom: 1px solid #e5e7eb; position: sticky; top: 60px; z-index: 99;
    }}
    .controls-label {{ font-size: 0.75rem; font-weight: 700; color: #6b7280; letter-spacing: 0.5px; }}
    .sort-btn {{
      padding: 6px 16px; border-radius: 99px; border: 1.5px solid #e5e7eb;
      background: #fff; font-size: 0.75rem; font-weight: 700; color: #6b7280; cursor: pointer; transition: all .15s;
    }}
    .sort-btn:hover {{ border-color: #16a34a; color: #16a34a; }}
    .sort-btn.active {{ background: #16a34a; border-color: #16a34a; color: #fff; }}
    .legend {{ margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: nowrap; }}
    .legend-item {{ display: flex; align-items: center; gap: 4px; font-size: 0.65rem; font-weight: 600; color: #6b7280; white-space: nowrap; }}
    .legend-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
    .legend-sep {{ width: 1px; height: 16px; background: #e5e7eb; margin: 0 2px; flex-shrink: 0; }}
    .tip-wrap {{ position: relative; display: inline-block; }}
    .legend-badge {{
      display: inline-block;
      padding: 3px 9px; border-radius: 99px;
      font-size: 0.6rem; font-weight: 700; letter-spacing: 0.3px;
      cursor: default; white-space: nowrap;
    }}
    .tip {{
      display: none; position: absolute;
      bottom: calc(100% + 7px); left: 50%; transform: translateX(-50%);
      background: #1f2937; color: #f9fafb;
      font-size: 0.6rem; font-weight: 400;
      padding: 5px 10px; border-radius: 6px;
      white-space: nowrap; pointer-events: none;
      box-shadow: 0 4px 12px rgba(0,0,0,0.2); z-index: 200;
    }}
    .tip-wrap:hover .tip {{ display: block; }}
    #cardContainer {{ max-width: 1180px; margin: 32px auto; padding: 0 32px 64px; }}
    .unmatched-divider {{
      display: flex; align-items: center; gap: 12px; margin: 40px 0 20px;
      color: #9ca3af; font-size: 0.7rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
    }}
    .unmatched-divider::before, .unmatched-divider::after {{ content: ''; flex: 1; height: 1px; background: #e5e7eb; }}
    .audit-card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.10), 0 16px 40px rgba(0,0,0,.09) !important; }}
  </style>
</head>
<body>

<div class='topbar'>
  <span class='topbar-title'>AI Evaluation - Herbarium Audit</span>
  <span class='topbar-score'>{n_images} specimens · {fmt_pct(overall)} accuracy</span>
</div>

<div class='hero'>
  <h1>Herbarium Audit</h1>
  <p class='hero-sub'>Standardized AI extraction evaluation · {n_images} specimen{'s' if n_images != 1 else ''}</p>
  <div class='stats-row'>
    <div class='stat-box'><div class='stat-val' style='color:#bbf7d0;'>{fmt_pct(overall)}</div><div class='stat-label'>Mean accuracy</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#e5e7eb;'>{n}</div><div class='stat-label'>Specimens</div></div>
    {images_stat_box}
    <div class='stat-box'><div class='stat-val' style='color:#86efac;'>{n_good}</div><div class='stat-label'>Excellent (≥85%)</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#fde68a;'>{n_ok}</div><div class='stat-label'>Acceptable (60–85%)</div></div>
    <div class='stat-box'><div class='stat-val' style='color:#fca5a5;'>{n_poor}</div><div class='stat-label'>Poor (&lt;60%)</div></div>
    {unmatched_stat_box}
  </div>
  <div class='hero-sub' style='margin:24px 0 8px;'>Accuracy by field</div>
  <div class='field-chart'>
    {field_accuracy_html}
  </div>
</div>

<div class='controls'>
  <span class='controls-label'>Sort by score:</span>
  <button class='sort-btn active' id='sortDesc' onclick='sortCards(false)'>↓ Highest first</button>
  <button class='sort-btn'        id='sortAsc'  onclick='sortCards(true)'>↑ Lowest first</button>
  <div class='legend'>
    <span class='legend-item'><span class='legend-dot' style='background:#22c55e;'></span>≥85%</span>
    <span class='legend-item'><span class='legend-dot' style='background:#f59e0b;'></span>60–85%</span>
    <span class='legend-item'><span class='legend-dot' style='background:#ef4444;'></span>&lt;60%</span>
    <span class='legend-sep'></span>
    <span class='legend-item'>Synonym via:</span>
    <span class='tip-wrap'>
      <span class='legend-badge' style='background:#dcfce7; color:#15803d;'>WFO</span>
      <span class='tip'>AI used an old synonym — GBIF name is current accepted (World Flora Online)</span>
    </span>
    <span class='tip-wrap'>
      <span class='legend-badge' style='background:#dbeafe; color:#1e40af;'>GBIF</span>
      <span class='tip'>Verified in GBIF verbatim or interpreted record</span>
    </span>
    <span class='tip-wrap'>
      <span class='legend-badge' style='background:#fef3c7; color:#a16207;'>WFO ↑</span>
      <span class='tip'>AI gave current accepted name — GBIF name is outdated (World Flora Online)</span>
    </span>
    <span class='tip-wrap'>
      <span class='legend-badge' style='background:#e0f2fe; color:#0369a1;'>WFO ≈</span>
      <span class='tip'>Both names are synonyms of the same accepted name (World Flora Online)</span>
    </span>
    <span class='legend-sep'></span>
    <span class='legend-item' style='font-size:0.8rem;'>⊘ excluded</span>
    <span class='legend-sep'></span>
    <button class='sort-btn' id='toggleMethods'
            onclick="const p=document.getElementById('methodsPanel');
                     const v=p.style.display==='none';
                     p.style.display=v?'block':'none';
                     this.classList.toggle('active',v);">
      ℹ scoring methods
    </button>
  </div>
</div>

<div id='methodsPanel' style='display:none; background:#f8fdf8; border-bottom:1px solid #e2ede2; padding:16px 48px;'>
  <div style='max-width:1100px; margin:0 auto;'>
    <div style='font-size:0.65rem; font-weight:700; letter-spacing:1px; text-transform:uppercase;
                color:#6b7280; margin-bottom:12px;'>How each field is scored</div>
    <div style='display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:8px 32px;'>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#d1f0e3;color:#1a7a4a;white-space:nowrap;flex-shrink:0;'>Taxon-Match</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Exact binomial — genus + epithet identical after stripping authors</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#dbeafe;color:#1565a8;white-space:nowrap;flex-shrink:0;'>token-match</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Collector name — any shared surname or initial = 100%</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#dbeafe;color:#1565a8;white-space:nowrap;flex-shrink:0;'>coll-cer</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Collector name, no shared token — reordering-tolerant sequence-match ratio instead of a raw character diff</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#cffafe;color:#0e7490;white-space:nowrap;flex-shrink:0;'>date-eval</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Precision-aware — year = 50%, year+month = 75–100%, full date = 100%</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#f3f4f6;color:#374151;white-space:nowrap;flex-shrink:0;'>cer</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Character Error Rate — 1 − (Levenshtein distance ÷ length). Default for free text.</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#d1fae5;color:#065f46;white-space:nowrap;flex-shrink:0;'>geo-hdx-match</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Country — standardised to ISO-3 code via HDX database before comparing</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#f3f4f6;color:#374151;white-space:nowrap;flex-shrink:0;'>geo-cer-bag</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Geography — sequence-match ratio over normalized text, tolerant of word reordering</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#e0f2fe;color:#0369a1;white-space:nowrap;flex-shrink:0;'>coord-eval</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>Haversine distance — &lt;1 km=100%, &lt;10 km=90%, &lt;50 km=30%, &lt;100 km=10%, ≥100 km=0%</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#f9fafb;color:#9ca3af;white-space:nowrap;flex-shrink:0;'>N/A</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>GBIF truth itself has no value for this field — not evaluable, excluded from the average</span>
      </div>
      <div style='display:flex; gap:8px; align-items:flex-start;'>
        <span style='display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.6rem;font-weight:700;background:#fee2e2;color:#b91c1c;white-space:nowrap;flex-shrink:0;'>missing</span>
        <span style='font-size:0.7rem;color:#6b7280;line-height:1.4;'>GBIF truth is available but the AI did not extract this field — scored 0%, counted against the average</span>
      </div>
    </div>
  </div>
</div>

<div id='cardContainer'>
  {cards_html}
</div>

<script>
function sortCards(ascending) {{
  const container = document.getElementById('cardContainer');
  const allCards  = Array.from(container.querySelectorAll('.audit-card'));
  const scored    = allCards.filter(c => parseFloat(c.dataset.score) >= 0);
  const unmatched = allCards.filter(c => parseFloat(c.dataset.score) < 0);

  scored.sort((a, b) => {{
    const sa = parseFloat(a.dataset.score);
    const sb = parseFloat(b.dataset.score);
    return ascending ? sa - sb : sb - sa;
  }});

  scored.forEach(c => container.appendChild(c));

  if (unmatched.length > 0) {{
    let divider = container.querySelector('.unmatched-divider');
    if (!divider) {{
      divider = document.createElement('div');
      divider.className = 'unmatched-divider';
      divider.textContent = '' + unmatched.length + ' specimen' + (unmatched.length > 1 ? 's' : '') + ' with no truth data';
    }}
    container.appendChild(divider);
    unmatched.forEach(c => container.appendChild(c));
  }}

  document.getElementById('sortAsc').classList.toggle('active', ascending);
  document.getElementById('sortDesc').classList.toggle('active', !ascending);
}}

sortCards(false);
</script>
</body>
</html>"""

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    html_path = str(Path(output_dir) / (f"results_{output_tag}.html" if output_tag else OUTPUT_HTML))
    xlsx_path = str(Path(output_dir) / (f"results_{output_tag}.xlsx" if output_tag else OUTPUT_XLSX))

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Excel layout:
    #   "Darwin Core"   one row per specimen: the AI transcription under Darwin Core term names
    #                   (coordinates exactly as transcribed) + taxonomicStatus / acceptedNameUsage.
    #   "Evaluation"    one row per specimen and field: truth, ai, score, method, note.
    #   "Field Summary" mean accuracy per field.
    def _pid(fname):
        return re.sub(r"\.(jpg|jpeg|png|JPG|PNG)$", "", str(fname), flags=re.IGNORECASE).strip()

    ai_by_id = {_pid(r.get("filename", "")): r for _, r in vv_df.iterrows()}
    all_ids  = [c[4] for c in matched_cards] + [c[4] for c in unmatched_cards]
    with ThreadPoolExecutor(max_workers=6) as pool:
        tax_by_id = dict(zip(all_ids, pool.map(
            lambda i: _taxonomic_check(str(ai_by_id[i].get("scientificName", "")) if i in ai_by_id else ""),
            all_ids)))
    _save_wfo_cache()

    scores_by_id = {c[4]: (c[5], round(c[0], 4)) for c in matched_cards}   # id -> (gbifID, overall score)
    dwc_rows = []
    for pure_id in all_ids:
        ai_row = ai_by_id.get(pure_id)
        gid, overall_score = scores_by_id.get(pure_id, (pure_id, None))
        row = {"filename": pure_id}
        for term in DWC_EXCEL_FIELDS:
            row[term] = str(ai_row.get(term, "")).strip() if ai_row is not None else ""
        status, accepted, according = tax_by_id.get(pure_id, ("", "", ""))
        row["taxonomicStatus"]   = status
        row["acceptedNameUsage"] = accepted
        row["nameAccordingTo"]   = according
        row["gbifID"]            = gid
        row["status"]            = "matched" if pure_id in scores_by_id else "unmatched"
        row["overall_score"]     = overall_score
        dwc_rows.append(row)

    eval_rows = []
    for avg, _, _, field_results, pure_id, gid in matched_cards:
        ai_row = ai_by_id.get(pure_id)
        for fr in field_results:
            field = _EXCEL_FIELD_NAME.get(fr["field"], fr["field"])
            ai_val = fr["ai"]
            if field == "coordinates" and ai_row is not None:   # raw transcription, not the formatted value
                ai_val = f"{str(ai_row.get('decimalLatitude', '')).strip()}, {str(ai_row.get('decimalLongitude', '')).strip()}"
            eval_rows.append({
                "filename": pure_id, "gbifID": gid, "field": field,
                "truth": fr["truth"], "ai": ai_val,
                "score": round(fr["score"], 4) if fr["score"] is not None else None,
                "method": fr["method"], "note": fr["note"],
            })

    field_summary_rows = [
        {
            "field":         _EXCEL_FIELD_NAME.get(fa["field"], fa["field"]),
            "mean_accuracy": round(fa["mean_accuracy"], 4) if fa["mean_accuracy"] is not None else None,
            "n_scored":      fa["n_scored"],
        }
        for fa in field_accuracy
    ]

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame(dwc_rows).to_excel(writer, sheet_name="Darwin Core", index=False)
        pd.DataFrame(eval_rows).to_excel(writer, sheet_name="Evaluation", index=False)
        pd.DataFrame(field_summary_rows).to_excel(writer, sheet_name="Field Summary", index=False)

    print(f"\n Audit complete → {html_path}")
    print(f"   Excel export  → {xlsx_path}")
    print(f"   Matched    : {n}  |  Excellent: {n_good}  Acceptable: {n_ok}  Poor: {n_poor}")
    print(f"   Images     : {n_images}")
    print(f"   No truth   : {n_unmatched}")
    print(f"   Mean accuracy : {fmt_pct(overall)}")
