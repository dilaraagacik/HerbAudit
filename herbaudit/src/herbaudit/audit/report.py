"""
HTML report building: image lookup, badges, per-specimen audit cards
(with-reference and no-reference variants).
"""
from __future__ import annotations

import base64
import math
import os
import re

import requests

from .text import clean_text, strip_authors, has_non_latin_script, transliterate
from .geo import coord_accuracy, _haversine_km, _geocode_location
from .scoring import get_accuracy, EXCLUDED_SENTINEL
from .wfo import wfo_status_badge_html


def get_img(fn, gid, local_folder=None):
    """
    Try to return an image for the specimen.
    1. Look for the file in local_folder (base64-encoded data URI).
    2. Fall back to GBIF media API only when gid looks like a numeric GBIF ID.
    """
    if local_folder and os.path.isdir(local_folder):
        candidates = [c for c in (fn, gid) if c and str(c).strip() not in ("nan", "none", "0", "")]
        for term in candidates:
            base_term = re.sub(r"\.(jpg|jpeg|png|tif|tiff|bmp|webp)$", "",
                               str(term), flags=re.IGNORECASE)
            # Strip path components so a crafted value (e.g. "../../etc/passwd") can't escape local_folder.
            base_term = os.path.basename(base_term)
            for ext in (".jpg", ".jpeg", ".JPG", ".png", ".PNG", ".tif", ".tiff"):
                path = os.path.join(local_folder, f"{base_term}{ext}")
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"

    # Fall back to GBIF media API (only for numeric GBIF occurrence IDs)
    if gid and re.fullmatch(r"\d+", str(gid).strip()):
        try:
            r = requests.get(
                f"https://api.gbif.org/v1/occurrence/{gid}",
                timeout=5,
            ).json()
            for m in r.get("media", []):
                if m.get("type") == "StillImage":
                    url = m.get("identifier", "").strip()
                    if url:
                        return url
        except Exception as e:
            print(f"GBIF image fetch failed for {gid}: {e}")

    return ""


def display_name(fn, gid):
    if fn  and fn  not in ("nan", "none", "0", ""):
        return fn
    if gid and gid not in ("nan", "none", "0", ""):
        return f"GBIF #{gid}"
    return "Unknown specimen"


BADGE_COLORS = {
    "Taxon-Match":   ("#1a7a4a", "#d1f0e3"),
    "Taxon-Synonym": ("#5b3dbd", "#ece8ff"),
    "token-match":   ("#1565a8", "#dbeafe"),
    "date-eval":     ("#0e7490", "#cffafe"),
    "alias-match":   ("#92400e", "#fef3c7"),
    "cer":           ("#374151", "#f3f4f6"),
    "coll-cer":      ("#1565a8", "#dbeafe"),
    "cer-manual-text": ("#7c3aed", "#ede9fe"),
    "N/A":           ("#9ca3af", "#f9fafb"),
    "missing":       ("#b91c1c", "#fee2e2"),
    "geo-match":     ("#065f46", "#d1fae5"),
    "geo-alias":     ("#065f46", "#d1fae5"),
    "geo-hdx-match": ("#065f46", "#d1fae5"),
    "geo-cer-bag":   ("#374151", "#f3f4f6"),
    "coord-eval":    ("#0369a1", "#e0f2fe"),
}


BADGE_TOOLTIPS = {
    "Taxon-Match":   "Exact binomial match — genus + specific epithet identical after stripping authors",
    "token-match":   "Collector token match — any shared surname or initial between AI and GBIF = 100%",
    "date-eval":     "Precision-aware date scoring — year match = 50%, year+month = 75-100%, full date = 100%",
    "cer":           "Character Error Rate — 1 - (Levenshtein distance / reference length). Score = similarity %",
    "coll-cer":      "Collector name CER — words paired by best match (tolerant of reordering, e.g. \"Smith, J.\" vs \"J. Smith\"), edits divided by the reference length; a matching initial is free",
    "cer-manual-text": "GBIF/AI mismatch resolved against the actual label transcription (Label Studio) — exact whole-word match only, decide which side (or neither) the sheet backs, then CER against that reference",
    "geo-hdx-match": "ISO country code match — standardised via HDX country database",
    "geo-cer-bag":   "Location similarity — words paired by best match, tolerant of reordering; reference words with no match are not penalized (GBIF truth). Against a manual annotation, CER divided by the reference length",
    "coord-eval":    "Haversine distance — <1 km=100%, <10 km=90%, <50 km=30%, <100 km=10%, ≥100 km=0%",
    "N/A":           "GBIF truth itself has no value for this field — not evaluable, excluded from the average",
    "missing":       "GBIF truth is available but the AI did not extract this field — scored 0%, counted against the average",
}


SYN_SRC_COLORS = {
    "WFO":       ("#15803d", "#dcfce7"),   # forward: AI used old synonym of GBIF accepted name
    "WFO-rev":   ("#a16207", "#fef3c7"),   # reverse: AI gave more current accepted name
    "WFO-sib":   ("#0369a1", "#e0f2fe"),   # sibling: both names are synonyms of same accepted name
    "GBIF":      ("#1e40af", "#dbeafe"),
    "GBIFbb":     ("#9d174d", "#fce7f3"),  # forward: AI used old synonym, per GBIF's own backbone
    "GBIFbb-rev": ("#b45309", "#fef3c7"),  # reverse: AI gave more current accepted name
    "GBIFbb-sib": ("#4d7c0f", "#ecfccb"),  # sibling: both names are synonyms of same accepted name
    "GBIFbb-eq":  ("#166534", "#dcfce7"),  # equivalent: both resolve to the same accepted usageKey (spelling variant)
    "NOREL":      ("#78716c", "#f5f5f4"),  # AI's name is real (GBIF-recognized) but unrelated to the truth name
}


# Human-readable display labels for synonym source badges
# (key → (short_label, tooltip))
SYN_SRC_DISPLAY = {
    "WFO":       ("via WFO",         "AI used an old synonym; GBIF name is the current accepted name (World Flora Online)"),
    "WFO-rev":   ("via WFO ↑",       "AI gave the current accepted name; GBIF name is outdated (World Flora Online)"),
    "WFO-sib":   ("via WFO ≈",       "Both names are synonyms of the same accepted name (World Flora Online)"),
    "GBIF":      ("via GBIF",   "Verified in GBIF verbatim or interpreted record"),
    "GBIFbb":     ("via GBIF backbone",   "AI used an old synonym; GBIF name is the current accepted name (GBIF's own backbone taxonomy)"),
    "GBIFbb-rev": ("via GBIF backbone ↑", "AI gave the current accepted name; GBIF name is outdated (GBIF's own backbone taxonomy)"),
    "GBIFbb-sib": ("via GBIF backbone ≈", "Both names are synonyms of the same accepted name (GBIF's own backbone taxonomy)"),
    "GBIFbb-eq":  ("via GBIF backbone =", "Both names resolve to the same accepted GBIF taxon — likely a spelling/orthographic variant of the same epithet"),
    "NOREL":      ("no relationship", "AI's name is real and recognized by GBIF, but is not a synonym or match of the GBIF truth name — likely a different or superseded determination"),
}


def make_badge(method):
    fg, bg  = BADGE_COLORS.get(method, ("#374151", "#f3f4f6"))
    tooltip = BADGE_TOOLTIPS.get(method, "")
    tip_attr = f" title='{tooltip}'" if tooltip else ""
    cursor   = "cursor:help; " if tooltip else ""
    return (
        f"<span{tip_attr} style='{cursor}display:inline-block; padding:2px 8px; border-radius:99px; "
        f"font-size:0.6rem; font-weight:700; letter-spacing:0.5px; "
        f"background:{bg}; color:{fg};'>{method}</span>"
    )


def fmt_pct(fraction: float, decimals: int = 2) -> str:
    """Percent display with trailing zeros trimmed — 0.6953 -> "69.53%",
    but 0.70 -> "70%" (not "70.00%") and 1.0 -> "100%" (not "100.00%")."""
    s = f"{fraction * 100:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{s}%"


def score_bar(score):
    if score is None:
        return ""
    pct   = int(score * 100)
    color = "#22c55e" if score >= 0.85 else ("#f59e0b" if score >= 0.60 else "#ef4444")
    return (
        f"<div style='width:100%; height:4px; background:#e5e7eb; border-radius:2px; margin-top:4px;'>"
        f"<div style='width:{pct}%; height:4px; background:{color}; border-radius:2px;'></div></div>"
    )


def translit_flag_html(value):
    """🌐 flag + Latin transliteration, shown under a non-Latin-script value
    (Cyrillic, Greek, CJK, ...) so a low CER score from script differences
    alone doesn't look like a plain wrong answer. "" if already Latin-script."""
    if not has_non_latin_script(value):
        return ""
    translit = transliterate(value)
    return (
        f"<div style='font-size:0.62rem; color:#6b7280; margin-top:2px; "
        f"font-style:italic;' title='Latin transliteration (unidecode)'>"
        f"🌐 {translit}</div>"
    )


COL_MAP = {
    "genus":           "truth_genus",
    "specificEpithet": "truth_specificEpithet",
    "scientificName":  "truth_scientificName",
    "recordedBy":      "truth_recordedBy",
    "eventDate":       "truth_eventDate",
    "catalogNumber":   "truth_catalogNumber",
    "country":         "truth_country",
    "stateProvince":   "truth_stateProvince",
    "locality":        "truth_locality",
}


def build_card(fname, gid, truth_row, ai_row, field_map, images_folder=None, manual_text=None,
               strict_geo=False):
    """Build an HTML audit card for one specimen.
    manual_text: sheet transcription, used only to resolve an eventDate
    mismatch that might be GBIF's fault (see get_accuracy).
    strict_geo: score geography as CER against the truth length (manual annotations).
    Returns (avg_score, html, has_image, field_results)."""
    gbif_url        = f"https://www.gbif.org/occurrence/{gid}"
    label           = display_name(fname, gid)
    rows_html       = ""
    card_scores     = []
    field_results   = []   # per-field records for the Excel export
    excluded_count  = 0
    taxon_is_synonym = False

    for group, fields in field_map.items():
        icon = {"Taxonomy": "", "Collector": "", "Geography": ""}.get(group, "")
        rows_html += (
            f"<tr><td colspan='5' style='padding:10px 8px 4px; font-size:0.6rem; "
            f"font-weight:800; letter-spacing:1.5px; color:#9ca3af; border:none; "
            f"text-transform:uppercase;'>{icon} {group}</td></tr>"
        )

        for vv_col, (is_tax, is_coll, is_date, is_geo) in fields.items():
            v_val = str(ai_row.get(vv_col, "N/A"))

            truth_key = COL_MAP.get(vv_col, f"truth_{vv_col}")
            g_val = str(truth_row[truth_key]) if (truth_row is not None and truth_key in truth_row) else "N/A"

            # If raw truth has an old hyphenated epithet (e.g. "brunneo-villosus")
            # but the AI gave the un-hyphenated form, fall back to the epithet
            # tokenized from truth_scientificName, which is already un-hyphenated.
            if vv_col in ("genus", "specificEpithet") and truth_row is not None:
                sci_truth = str(truth_row.get("truth_scientificName", ""))
                binomial = strip_authors(sci_truth).split()
                if len(binomial) >= 2:
                    derived = binomial[0] if vv_col == "genus" else binomial[1]
                    clean_g = strip_authors(g_val)
                    clean_v = strip_authors(v_val)
                    if clean_g != clean_v and derived == clean_v:
                        g_val = derived

            display_col = "Generic Name" if vv_col == "genus" else vv_col

            # If scientificName was already excluded as a synonym, skip get_accuracy
            # for genus/specificEpithet — they're auto-excluded too, except a missing
            # genus is still scored as a real miss (specificEpithet stays excluded).
            if taxon_is_synonym and is_tax and vv_col in ("genus", "specificEpithet"):
                if vv_col == "genus" and clean_text(v_val) == "na":
                    acc, method, color, note = (
                        0.0, "missing", "#f8d7da", "AI did not extract this field"
                    )
                else:
                    acc, method, color, note = (
                        None, EXCLUDED_SENTINEL, "#fff8e1",
                        "Excluded (inherited from binomial synonym)"
                    )
            else:
                acc, method, color, note = get_accuracy(
                    g_val, v_val, is_tax, is_coll, is_date, is_geo,
                    gbif_meta=truth_row, manual_text=manual_text,
                    is_epithet=(vv_col == "specificEpithet"), strict_geo=strict_geo
                )

            if vv_col == "scientificName" and method == EXCLUDED_SENTINEL:
                taxon_is_synonym = True

            wfo_badge_html = wfo_status_badge_html(v_val) if vv_col == "scientificName" else ""

            if method == EXCLUDED_SENTINEL:
                excluded_count += 1
                field_results.append({
                    "field": display_col, "truth": g_val, "ai": v_val,
                    "method": "synonym-excluded", "score": None, "note": note.split("||", 1)[-1],
                })
                syn_src, syn_note_text = note.split("||", 1) if "||" in note else (" ", note)
                src_fg, src_bg     = SYN_SRC_COLORS.get(syn_src, ("#374151", "#f3f4f6"))
                src_label, src_tooltip = SYN_SRC_DISPLAY.get(syn_src, (f"via {syn_src}", "")) if syn_src != " " else (" ", "")

                # Badge label, text colour and row stripe vary by case
                # (suffix-based so WFO-*/GBIFbb-* sources share styling)
                if syn_src.endswith("-rev"):
                    syn_label   = "⊘ AI more current"
                    label_fg    = "#6d28d9"; label_bg_c = "#ede9fe"
                    note_color  = "#6d28d9"
                    stripe_a    = "#f5f3ff"; stripe_b   = "#ede9fe"
                elif syn_src.endswith("-sib"):
                    syn_label   = "⊘ sibling synonym"
                    label_fg    = "#0f766e"; label_bg_c = "#ccfbf1"
                    note_color  = "#0f766e"
                    stripe_a    = "#f0fdfa"; stripe_b   = "#ccfbf1"
                elif syn_src == "NOREL":
                    syn_label   = "⊘ no relationship"
                    label_fg    = "#b91c1c"; label_bg_c = "#fee2e2"
                    note_color  = "#b91c1c"
                    stripe_a    = "#fef2f2"; stripe_b   = "#fee2e2"
                else:
                    syn_label   = "⊘ synonym"
                    label_fg    = "#92400e"; label_bg_c = "#fef3c7"
                    note_color  = "#b45309"
                    stripe_a    = "#fffdf0"; stripe_b   = "#fffbeb"

                rows_html += f"""
                <tr style='border-bottom:1px solid #f3f4f6;
                    background:repeating-linear-gradient(45deg,{stripe_a},{stripe_a} 8px,{stripe_b} 8px,{stripe_b} 16px);'>
                  <td style='padding:8px; font-size:0.72rem; font-weight:600; color:{label_fg}; width:18%;'>{display_col}</td>
                  <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%;'>{g_val}</td>
                  <td style='padding:8px; font-size:0.7rem; font-family:monospace; color:#1f2937; width:23%;'>{v_val}</td>
                  <td style='padding:8px; width:20%;'>
                    <div style='display:flex; align-items:center; gap:5px; flex-wrap:wrap;'>
                      <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                          font-size:0.6rem; font-weight:700; background:{label_bg_c}; color:{label_fg};'>{syn_label}</span>
                      <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                          font-size:0.6rem; font-weight:700; background:{src_bg}; color:{src_fg};
                          cursor:default;' title='{src_tooltip}'>{src_label}</span>
                      {wfo_badge_html}
                    </div>
                    <div style='font-size:0.6rem; color:{note_color}; margin-top:4px; font-style:italic; line-height:1.4;'>{syn_note_text}</div>
                  </td>
                  <td style='padding:8px; width:16%; text-align:center;'>
                    <div style='font-size:0.75rem; font-weight:700; color:#d97706;'>excl.</div>
                    <div style='font-size:0.6rem; color:#9ca3af;'>not scored</div>
                  </td>
                </tr>"""
                continue

            if method != "N/A":
                card_scores.append(acc)

            field_results.append({
                "field": display_col, "truth": g_val, "ai": v_val,
                "method": method, "score": acc if method != "N/A" else None, "note": note,
            })

            res_display = fmt_pct(acc) if method != "N/A" else "—"
            res_color   = (
                "#22c55e" if acc >= 0.85 else ("#f59e0b" if acc >= 0.60 else "#ef4444")
            ) if method != "N/A" else "#9ca3af"

            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#374151; width:18%;'>{display_col}</td>
              <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%;'>{g_val}{translit_flag_html(g_val)}</td>
              <td style='padding:8px; font-size:0.7rem; font-family:monospace; color:#1f2937; width:23%;'>{v_val}{translit_flag_html(v_val)}</td>
              <td style='padding:8px; width:20%;'>
                {make_badge(method)}
                {' '+wfo_badge_html if wfo_badge_html else ''}
                {'<div style="font-size:0.6rem;color:#6b7280;margin-top:2px;">'+note+'</div>' if note else ''}
              </td>
              <td style='padding:8px; width:16%; text-align:right;'>
                <span style='font-size:0.85rem; font-weight:800; color:{res_color};'>{res_display}</span>
                {score_bar(acc) if method != "N/A" else ""}
              </td>
            </tr>"""


    # Coordinates are handled separately from field_map — always shown when
    # GBIF has them, scored only when the AI also extracted them.
    t_lat  = truth_row.get("truth_decimalLatitude",  "N/A") if truth_row else "N/A"
    t_lon  = truth_row.get("truth_decimalLongitude", "N/A") if truth_row else "N/A"

    def _is_real_coord(lat, lon):
        """Return False for missing values AND for 0.0,0.0 (Null Island placeholder)."""
        try:
            if str(lat).lower() in ("n/a", "nan", "none", "", "null"):
                return False
            if str(lon).lower() in ("n/a", "nan", "none", "", "null"):
                return False
            # Treat exact 0,0 as a GBIF placeholder — no real herbarium specimen
            # is collected at Null Island (0°N 0°E in the Atlantic Ocean)
            return not (float(lat) == 0.0 and float(lon) == 0.0)
        except (ValueError, TypeError):
            return False

    gbif_has_coords = _is_real_coord(t_lat, t_lon)

    # Try several field names the AI extraction might use
    def _get_coord(row, *keys):
        for k in keys:
            v = str(row.get(k, "")).strip()
            if v and v.lower() not in ("n/a", "nan", "none", ""):
                return v
        return None

    if gbif_has_coords:
        rows_html += (
            "<tr><td colspan='5' style='padding:10px 8px 4px; font-size:0.6rem; "
            "font-weight:800; letter-spacing:1.5px; color:#9ca3af; border:none; "
            "text-transform:uppercase;'>Coordinates</td></tr>"
        )

        ai_lat = _get_coord(ai_row, "decimalLatitude",  "latitude",  "lat")
        ai_lon = _get_coord(ai_row, "decimalLongitude", "longitude", "lon")
        # Reject 0,0 from AI too — same null island reasoning
        ai_has_coords = (
            ai_lat is not None and ai_lon is not None
            and _is_real_coord(ai_lat, ai_lon)
        )

        gbif_coord_str = f"{float(t_lat):.5f}, {float(t_lon):.5f}"

        if ai_has_coords:
            # Both sides have coordinates — score them
            ai_coord_str = f"{float(ai_lat):.5f}, {float(ai_lon):.5f}"
            score = coord_accuracy(t_lat, t_lon, ai_lat, ai_lon)

            if score is None:
                # Unparseable AI value — treat as wrong, show raw
                ai_coord_str = f"{ai_lat}, {ai_lon}"
                coord_row_color = "#f8d7da"
                score_display   = "—"
                score_color_c   = "#9ca3af"
                badge_html      = make_badge("cer")
                bar_html        = ""
            else:
                card_scores.append(score)
                dist_km = _haversine_km(float(t_lat), float(t_lon), float(ai_lat), float(ai_lon))
                coord_row_color = "#d1e7dd" if score >= 0.85 else "#f8d7da"
                score_display   = fmt_pct(score)
                score_color_c   = "#22c55e" if score >= 0.85 else ("#f59e0b" if score >= 0.60 else "#ef4444")
                badge_html      = (
                    f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                    f"font-size:0.6rem; font-weight:700; background:#e0f2fe; color:#0369a1;'>"
                    f"coord-eval</span>"
                    f"<div style='font-size:0.6rem; color:#6b7280; margin-top:2px;'>"
                    f"{dist_km:.1f} km off</div>"
                )
                bar_html = score_bar(score)

            field_results.append({
                "field": "coordinates", "truth": gbif_coord_str, "ai": ai_coord_str,
                "method": "coord-eval", "score": score, "note": "",
            })

            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb; background:{coord_row_color}18;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#374151; width:18%;'>coordinates</td>
              <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%;'>{gbif_coord_str}</td>
              <td style='padding:8px; font-size:0.7rem; font-family:monospace; color:#1f2937; width:23%;'>{ai_coord_str}</td>
              <td style='padding:8px; width:20%;'>{badge_html}</td>
              <td style='padding:8px; width:16%; text-align:right;'>
                <span style='font-size:0.85rem; font-weight:800; color:{score_color_c};'>{score_display}</span>
                {bar_html}
              </td>
            </tr>"""

        else:
            # AI didn't extract coordinates — show GBIF coords as inferred;
            # the AI's column stays "not extracted", never a geocoded guess.
            field_results.append({
                "field": "coordinates", "truth": gbif_coord_str, "ai": "not extracted",
                "method": "inferred-gbif", "score": None, "note": "",
            })
            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb; background:#f0f9ff;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#0369a1; width:18%;'>coordinates</td>
              <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%;'>{gbif_coord_str}</td>
              <td style='padding:8px; font-size:0.7rem; color:#94a3b8; font-family:monospace; width:23%; font-style:italic;'>not extracted</td>
              <td style='padding:8px; width:20%;'>
                <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                    font-size:0.6rem; font-weight:700; background:#e0f2fe; color:#0369a1;'
                    title='Coordinates not extracted by AI — shown from GBIF for reference only'>
                  inferred from GBIF
                </span>
              </td>
              <td style='padding:8px; width:16%; text-align:center;'>
                <div style='font-size:0.75rem; font-weight:700; color:#94a3b8;'>—</div>
                <div style='font-size:0.6rem; color:#cbd5e1;'>not scored</div>
              </td>
            </tr>"""

    else:
        # GBIF has no coordinates — geocode its locality/stateProvince/country
        # text as a best-effort estimate for the truth side only. Always
        # informational, never scored: it's a rough guess from free text, not
        # real GBIF ground truth.
        ai_lat_nb = _get_coord(ai_row, "decimalLatitude",  "latitude",  "lat")
        ai_lon_nb = _get_coord(ai_row, "decimalLongitude", "longitude", "lon")
        ai_has_coords_nb = (
            ai_lat_nb is not None and ai_lon_nb is not None
            and _is_real_coord(ai_lat_nb, ai_lon_nb)
        )

        loc_locality = str(truth_row.get("truth_locality",      "")) if truth_row else ""
        loc_state    = str(truth_row.get("truth_stateProvince", "")) if truth_row else ""
        loc_country  = str(truth_row.get("truth_country",       "")) if truth_row else ""
        geo_result = _geocode_location(loc_locality, loc_state, loc_country)

        rows_html += (
            "<tr><td colspan='5' style='padding:10px 8px 4px; font-size:0.6rem; "
            "font-weight:800; letter-spacing:1.5px; color:#9ca3af; border:none; "
            "text-transform:uppercase;'>Coordinates</td></tr>"
        )

        if geo_result:
            geo_lat, geo_lon, geo_display = geo_result
            geo_coord_str = f"{float(geo_lat):.5f}, {float(geo_lon):.5f}"
            source_parts = [p for p in [loc_locality, loc_state, loc_country]
                            if p and p.lower() not in ("n/a", "nan", "none", "")]
            source_label = ", ".join(source_parts) if source_parts else "location fields"

            if ai_has_coords_nb:
                # AI has real coordinates — show the geocoded GBIF-side estimate
                # alongside it for reference only, never scored against it.
                ai_coord_str_nb = f"{float(ai_lat_nb):.5f}, {float(ai_lon_nb):.5f}"
                field_results.append({
                    "field": "coordinates", "truth": f"{geo_coord_str} (geocoded)", "ai": ai_coord_str_nb,
                    "method": "geocoded-inferred", "score": None, "note": source_label,
                })
                rows_html += f"""
                <tr style='border-bottom:1px solid #f9fafb; background:#fafaf9;'>
                  <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#78716c; width:18%;'>coordinates</td>
                  <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%; font-style:italic;'
                      title='{geo_display}'>{geo_coord_str} (geocoded)</td>
                  <td style='padding:8px; font-size:0.7rem; font-family:monospace; color:#1f2937; width:23%;'>{ai_coord_str_nb}</td>
                  <td style='padding:8px; width:20%;'>
                    <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                        font-size:0.6rem; font-weight:700; background:#f5f5f4; color:#78716c;'
                        title='GBIF has no coordinates. Geocoded from GBIF locality: {source_label}&#10;Via Nominatim / OpenStreetMap — approximate only, not scored against the AI'>
                      geocoded (not GBIF truth)
                    </span>
                    <div style='font-size:0.6rem; color:#a8a29e; margin-top:3px; font-style:italic; line-height:1.4;'
                        title='{geo_display}'>{source_label}</div>
                  </td>
                  <td style='padding:8px; width:16%; text-align:center;'>
                    <div style='font-size:0.75rem; font-weight:700; color:#a8a29e;'>—</div>
                    <div style='font-size:0.6rem; color:#d6d3d1;'>not scored</div>
                  </td>
                </tr>"""
            else:
                # Neither has coordinates — geocoded GBIF-side estimate, informational only
                field_results.append({
                    "field": "coordinates", "truth": f"{geo_coord_str} (geocoded)", "ai": "not extracted",
                    "method": "geocoded-inferred", "score": None, "note": source_label,
                })
                rows_html += f"""
                <tr style='border-bottom:1px solid #f9fafb; background:#fafaf9;'>
                  <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#78716c; width:18%;'>coordinates</td>
                  <td style='padding:8px; font-size:0.7rem; color:#78716c; font-family:monospace; width:23%; font-style:italic;'
                      title='Not real GBIF truth — geocoded from GBIF locality text'>{geo_coord_str} (geocoded)</td>
                  <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%; font-style:italic;'>not extracted</td>
                  <td style='padding:8px; width:20%;'>
                    <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                        font-size:0.6rem; font-weight:700; background:#f5f5f4; color:#78716c;'
                        title='Geocoded from GBIF locality: {source_label}&#10;Via Nominatim / OpenStreetMap — approximate only, not authoritative GBIF coordinates'>
                      geocoded (not GBIF truth)
                    </span>
                    <div style='font-size:0.6rem; color:#a8a29e; margin-top:3px; font-style:italic; line-height:1.4;'
                        title='{geo_display}'>{source_label}</div>
                  </td>
                  <td style='padding:8px; width:16%; text-align:center;'>
                    <div style='font-size:0.75rem; font-weight:700; color:#a8a29e;'>—</div>
                    <div style='font-size:0.6rem; color:#d6d3d1;'>not scored</div>
                  </td>
                </tr>"""

        elif ai_has_coords_nb:
            # Geocoding failed too, but AI has coordinates — nothing to compare against
            ai_coord_str_nb = f"{float(ai_lat_nb):.5f}, {float(ai_lon_nb):.5f}"
            field_results.append({
                "field": "coordinates", "truth": "no GBIF ref", "ai": ai_coord_str_nb,
                "method": "ai-extracted-unref", "score": None, "note": "",
            })
            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb; background:#f0fdf4;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#166534; width:18%;'>coordinates</td>
              <td style='padding:8px; font-size:0.7rem; color:#9ca3af; font-family:monospace; width:23%; font-style:italic;'>no GBIF ref</td>
              <td style='padding:8px; font-size:0.7rem; font-family:monospace; color:#1f2937; width:23%;'>{ai_coord_str_nb}</td>
              <td style='padding:8px; width:20%;'>
                <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                    font-size:0.6rem; font-weight:700; background:#dcfce7; color:#166534;'>
                  AI extracted
                </span>
                <div style='font-size:0.6rem; color:#86efac; margin-top:3px;'>no GBIF coords to compare</div>
              </td>
              <td style='padding:8px; width:16%; text-align:center;'>
                <div style='font-size:0.75rem; font-weight:700; color:#a8a29e;'>—</div>
                <div style='font-size:0.6rem; color:#d6d3d1;'>not scored</div>
              </td>
            </tr>"""

        else:
            # Nothing available at all — show placeholder so the row is never silent
            field_results.append({
                "field": "coordinates", "truth": "N/A", "ai": "N/A",
                "method": "N/A", "score": None, "note": "",
            })
            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb; background:#fafafa;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#9ca3af; width:18%;'>coordinates</td>
              <td style='padding:8px; font-size:0.7rem; color:#d1d5db; font-style:italic; width:23%;'>—</td>
              <td style='padding:8px; font-size:0.7rem; color:#d1d5db; font-style:italic; width:23%;'>—</td>
              <td style='padding:8px; width:20%;'>
                <span style='display:inline-block; padding:2px 8px; border-radius:99px;
                    font-size:0.6rem; font-weight:700; background:#f3f4f6; color:#9ca3af;'>
                  no coordinates
                </span>
              </td>
              <td style='padding:8px; width:16%; text-align:center;'>
                <div style='font-size:0.75rem; font-weight:700; color:#d1d5db;'>—</div>
              </td>
            </tr>"""

    avg         = sum(card_scores) / len(card_scores) if card_scores else 0.0
    score_color = "#22c55e" if avg >= 0.85 else ("#f59e0b" if avg >= 0.60 else "#ef4444")
    score_label = "Excellent" if avg >= 0.85 else ("Acceptable" if avg >= 0.60 else "Poor")
    ring_pct    = int(avg * 100)

    excl_tag = ""
    if excluded_count:
        excl_tag = (
            f"<span style='font-size:0.6rem; background:#fef3c7; color:#92400e; "
            f"border:1px solid #fde68a; border-radius:99px; padding:2px 8px; margin-left:6px; font-weight:600;'>"
            f"⊘ {excluded_count} synonym{'s' if excluded_count > 1 else ''} excluded</span>"
        )

    img_b64   = get_img(fname, gid, local_folder=images_folder)
    has_image = bool(img_b64)
    img_html  = (
        f"<img src='{img_b64}' style='max-height:440px; max-width:100%; "
        f"object-fit:contain; border-radius:8px; display:block; margin:auto;'>"
        if img_b64 else
        "<div style='height:200px; display:flex; align-items:center; "
        "justify-content:center; color:#d1d5db; font-size:0.85rem;'>No image</div>"
    )

    CIRC      = 188.5
    dash_fill = min(ring_pct * (CIRC / 100), CIRC)

    card = f"""
    <div class='audit-card' data-score='{avg:.6f}' style='
        background:#fff; border-radius:16px; margin-bottom:32px;
        box-shadow:0 1px 3px rgba(0,0,0,.07), 0 8px 24px rgba(0,0,0,.06);
        overflow:hidden; transition:box-shadow .2s;'>

      <div style='display:flex; align-items:center; justify-content:space-between;
          padding:18px 24px; border-bottom:1px solid #f3f4f6;
          background:linear-gradient(135deg,#f0fdf4 0%,#fff 100%);'>
        <div>
          <div style='font-size:0.65rem; font-weight:700; letter-spacing:1.5px;
              color:#9ca3af; text-transform:uppercase; margin-bottom:2px;'>Specimen</div>
          <h3 style='margin:0; font-size:1rem; font-weight:700; color:#111827;
              font-family:"IBM Plex Mono",monospace;'>{label} {excl_tag}</h3>
          <a href='{gbif_url}' target='_blank' style='font-size:0.7rem; color:#16a34a;
              text-decoration:none; font-weight:600; margin-top:4px; display:inline-block;'>
            ↗ View on GBIF
          </a>
        </div>
        <div style='text-align:center; min-width:90px;'>
          <svg width='72' height='72' viewBox='0 0 72 72'>
            <circle cx='36' cy='36' r='30' fill='none' stroke='#f3f4f6' stroke-width='7'/>
            <circle cx='36' cy='36' r='30' fill='none' stroke='{score_color}' stroke-width='7'
              stroke-dasharray='{dash_fill:.2f} {CIRC}'
              stroke-linecap='round' transform='rotate(-90 36 36)'/>
            <text x='36' y='36' dominant-baseline='central' text-anchor='middle'
              font-size='13' font-weight='800' fill='{score_color}'
              font-family="system-ui">{ring_pct}%</text>
          </svg>
          <div style='font-size:0.6rem; font-weight:700; color:#9ca3af;
              letter-spacing:0.5px; margin-top:2px;'>{score_label.upper()}</div>
        </div>
      </div>

      <div style='display:grid; grid-template-columns:1fr 280px;'>
        <div style='padding:0 8px; overflow:auto;'>
          <table style='width:100%; border-collapse:collapse;'>
            <thead>
              <tr style='border-bottom:2px solid #f3f4f6;'>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:18%;'>Field</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:23%;'>GBIF (truth)</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:23%;'>AI extraction</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:20%;'>Method</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:right; width:16%;'>Score</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        <div style='border-left:1px solid #f3f4f6; padding:20px;
            display:flex; align-items:center; justify-content:center; background:#fafafa;'>
          {img_html}
        </div>
      </div>
    </div>"""

    return avg, card, has_image, field_results


def _make_unmatched_card(fname, gid):
    """Build a placeholder card for specimens with no truth data found."""
    label    = display_name(fname, gid)
    gbif_url = f"https://www.gbif.org/occurrence/{gid}"
    return f"""
    <div class='audit-card unmatched-card' data-score='-1' style='
        background:#fff; border-radius:16px; margin-bottom:32px;
        box-shadow:0 1px 3px rgba(0,0,0,.07), 0 4px 12px rgba(0,0,0,.04);
        overflow:hidden; border:1.5px dashed #e5e7eb; opacity:0.75;'>
      <div style='display:flex; align-items:center; gap:14px; padding:18px 24px;
          background:repeating-linear-gradient(45deg,#fafafa,#fafafa 8px,#f3f4f6 8px,#f3f4f6 16px);'>
        <div style='width:42px; height:42px; border-radius:50%; background:#f3f4f6;
            display:flex; align-items:center; justify-content:center;
            font-size:1.2rem; flex-shrink:0;'></div>
        <div>
          <div style='font-size:0.65rem; font-weight:700; letter-spacing:1.5px;
              color:#9ca3af; text-transform:uppercase; margin-bottom:2px;'>No truth data found</div>
          <h3 style='margin:0; font-size:1rem; font-weight:700; color:#6b7280;
              font-family:"IBM Plex Mono",monospace;'>{label}</h3>
          <a href='{gbif_url}' target='_blank' style='font-size:0.7rem; color:#9ca3af;
              text-decoration:none; font-weight:600; margin-top:4px; display:inline-block;'>
            ↗ View on GBIF
          </a>
        </div>
        <div style='margin-left:auto; font-size:0.75rem; font-weight:700;
            color:#9ca3af; font-family:"IBM Plex Mono",monospace;'>not evaluated</div>
      </div>
    </div>"""


def _gemini_image_tokens(width: int, height: int, max_dim: int | None = None) -> int:
    """
    Estimate Gemini vision token count for an image.
    Images are tiled at 768×768 px; each tile = 258 tokens.
    Gemini hard-caps the long side at 3072 px before tiling.
    Pass max_dim to simulate a lower resolution.
    """
    w, h = max(int(width), 1), max(int(height), 1)
    if max_dim:
        scale = min(max_dim / max(w, h), 1.0)
        w, h  = max(1, int(w * scale)), max(1, int(h * scale))
    if max(w, h) > 3072:
        scale = 3072 / max(w, h)
        w, h  = max(1, int(w * scale)), max(1, int(h * scale))
    return max(math.ceil(w / 768) * math.ceil(h / 768), 1) * 258


# (input_rate, output_rate) in USD per single token
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30e-6, 2.50e-6),  # standard tier, global
    "gemini-2.5-flash": (0.15e-6,  0.60e-6),
    "gemini-2.5-pro":   (1.25e-6, 10.00e-6),
    "gemini-2.0-flash": (0.10e-6,  0.40e-6),
    "gemini-1.5-flash": (0.075e-6, 0.30e-6),
    "gemini-1.5-pro":   (1.25e-6,  5.00e-6),
}


_NOREF_FIELDS = [
    # (ai_col,                       display_label,             section)
    # Darwin Core fields only — kept in sync with HERBARIUM_SCHEMA (herbaudit/functions.py)
    ("scientificName",               "Scientific name",         "Taxonomy"),
    ("genus",                        "Genus",                   "Taxonomy"),
    ("specificEpithet",              "Species epithet",         "Taxonomy"),
    ("scientificNameAuthorship",     "Authorship",              "Taxonomy"),
    ("identifiedBy",                 "Identified by",           "Taxonomy"),
    ("dateIdentified",               "Identified date",         "Taxonomy"),
    ("recordedBy",                   "Collected by",            "Collector"),
    ("recordNumber",                 "Collector number",        "Collector"),
    ("verbatimEventDate",            "Date (verbatim)",         "Collector"),
    ("eventDate",                    "Collection date",         "Collector"),
    ("catalogNumber",                "Catalog number",          "Collector"),
    ("habitat",                      "Habitat",                 "Collector"),
    ("continent",                    "Continent",               "Geography"),
    ("country",                      "Country",                 "Geography"),
    ("stateProvince",                "State / Province",        "Geography"),
    ("county",                       "County",                  "Geography"),
    ("locality",                     "Locality",                "Geography"),
    ("verbatimCoordinates",          "Coordinates (verbatim)",  "Geography"),
    ("decimalLatitude",              "Latitude",                "Geography"),
    ("decimalLongitude",             "Longitude",               "Geography"),
    ("minimumElevationInMeters",     "Min. elevation (m)",      "Geography"),
    ("maximumElevationInMeters",     "Max. elevation (m)",      "Geography"),
]

_SECTION_ICON = {"Taxonomy": "", "Collector": "", "Geography": ""}


def build_no_reference_card(fname, ai_row, tax_info, img_meta, images_folder=None):
    """
    Build an HTML card for --no_reference mode.
    Shows all extracted fields, WFO/GBIF taxonomy status, cost and resolution info.
    Returns (cost_usd: float, html: str).
    """
    label = fname if fname and fname not in ("nan", "none", "") else "Unknown specimen"

    img_src  = get_img(fname, fname, local_folder=images_folder)
    img_html = (
        f"<img src='{img_src}' style='max-height:360px; max-width:100%; "
        f"object-fit:contain; border-radius:8px; display:block; margin:auto;'>"
        if img_src else
        "<div style='height:120px; display:flex; align-items:center; "
        "justify-content:center; color:#d1d5db; font-size:0.8rem;'>No image</div>"
    )

    w          = img_meta.get("width")
    h          = img_meta.get("height")
    cost_usd   = img_meta.get("cost_usd")
    in_tokens  = img_meta.get("input_tokens")
    out_tokens = img_meta.get("output_tokens")
    model_name = img_meta.get("model", "")

    res_str = f"{w} × {h} px" if (w and h) else "—"
    def _is_valid(v):
        try: return v is not None and float(v) == float(v)  # rejects None and NaN
        except (TypeError, ValueError): return False

    cost_usd    = float(cost_usd)   if _is_valid(cost_usd)   else None
    in_tokens   = int(in_tokens)    if _is_valid(in_tokens)   else None
    out_tokens  = int(out_tokens)   if _is_valid(out_tokens)  else None

    cost_str    = f"${cost_usd:.5f}" if cost_usd   is not None else "—"
    in_tok_str  = f"{in_tokens:,}"  if in_tokens   is not None else "—"
    out_tok_str = f"{out_tokens:,}" if out_tokens  is not None else "—"

    def _taxonomy_badge():
        if tax_info is None:
            return ""
        accepted = tax_info.get("accepted")
        is_syn   = tax_info.get("is_synonym", False)

        # WFO accepted/synonym badge. Currently always "not found" while
        # _WFO_ENABLED is False — WFO's API has no taxonomicStatus field to
        # reliably distinguish "accepted" from other statuses.
        if accepted is None:
            status_html = ("<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         "font-size:0.6rem; font-weight:700; background:#f3f4f6; color:#9ca3af;'>"
                         "? not found in WFO</span>")
        elif is_syn:
            status_html = (f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         f"font-size:0.6rem; font-weight:700; background:#fef3c7; color:#a16207;' "
                         f"title='Accepted name: {accepted}'>↑ WFO synonym → {accepted}</span>")
        else:
            status_html = ("<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         "font-size:0.6rem; font-weight:700; background:#dcfce7; color:#15803d;'>"
                         "WFO accepted</span>")

        # GBIF fuzzy match badge — shown when GBIF suggests a corrected name (≥85% confidence)
        gbif_fuzzy_accepted = tax_info.get("gbif_fuzzy_accepted")
        gbif_fuzzy_sim      = tax_info.get("gbif_fuzzy_sim", 0.0)
        source_name  = tax_info.get("source_name", "")

        gbif_fuzzy_html = ""
        if gbif_fuzzy_accepted and gbif_fuzzy_accepted != source_name and gbif_fuzzy_sim >= 0.85:
            sim_pct  = int(gbif_fuzzy_sim * 100)
            gbif_fuzzy_html = (
                f"<div style='display:inline-block; margin-top:3px; padding:2px 8px; "
                f"border-radius:99px; font-size:0.6rem; font-weight:700; "
                f"background:#fef9c3; color:#854d0e;' "
                f"title='GBIF fuzzy match — {sim_pct}% confidence'>"
                f"GBIF suggests: <i>{gbif_fuzzy_accepted}</i> ({sim_pct}%)</div>"
            )

        # WFO existence badge — independent of the accepted/synonym status
        # above; shows the closest candidate as a guess when there's no exact match.
        wfo_found       = tax_info.get("wfo_found")
        wfo_hierarchy   = tax_info.get("wfo_hierarchy")
        wfo_closest     = tax_info.get("wfo_closest")
        wfo_closest_sim = tax_info.get("wfo_closest_sim")

        # The badge above already says "WFO accepted" for this exact name —
        # showing "found in WFO" again underneath would just repeat it.
        wfo_already_shown = (not is_syn and accepted is not None)

        wfo_found_html = ""
        if wfo_already_shown:
            pass
        elif wfo_found:
            title = f" title='{wfo_hierarchy}'" if wfo_hierarchy else ""
            wfo_found_html = (f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                        f"font-size:0.6rem; font-weight:700; background:#e0f2fe; color:#0369a1; "
                        f"margin-top:3px;'{title}>found in WFO</span>")
        elif wfo_closest:
            sim_pct = int(wfo_closest_sim * 100)
            wfo_found_html = (f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                        f"font-size:0.6rem; font-weight:700; background:#f0f9ff; color:#0284c7; "
                        f"margin-top:3px;' title='Closest WFO name — {sim_pct}% match, not confirmed'>"
                        f"≈ WFO closest: <i>{wfo_closest}</i> ({sim_pct}%)</span>")

        # GBIF badge — only shown when WFO found nothing and GBIF was used as fallback
        gbif_status   = tax_info.get("gbif_status")
        gbif_accepted = tax_info.get("gbif_accepted", "")
        if gbif_status is None:
            extras = gbif_fuzzy_html + wfo_found_html
            return f"<div style='display:flex; flex-direction:column; gap:3px;'>{status_html}{extras}</div>" if extras else status_html
        if gbif_status == "ACCEPTED":
            gbif_html = ("<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         "font-size:0.6rem; font-weight:700; background:#dbeafe; color:#1e40af; margin-top:3px;'>"
                         "GBIF accepted</span>")
        elif gbif_status == "SYNONYM":
            _acc_label = f" → {gbif_accepted}" if gbif_accepted else ""
            gbif_html = (f"<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         f"font-size:0.6rem; font-weight:700; background:#fef3c7; color:#92400e; margin-top:3px;' "
                         f"title='Accepted: {gbif_accepted}'>↑ GBIF synonym{_acc_label}</span>")
        elif gbif_status == "DOUBTFUL":
            gbif_html = ("<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         "font-size:0.6rem; font-weight:700; background:#fee2e2; color:#b91c1c; margin-top:3px;'>"
                         "GBIF doubtful</span>")
        else:
            gbif_html = ("<span style='display:inline-block; padding:2px 8px; border-radius:99px; "
                         "font-size:0.6rem; font-weight:700; background:#f3f4f6; color:#9ca3af; margin-top:3px;'>"
                         "? not in GBIF</span>")

        return f"<div style='display:flex; flex-direction:column; gap:3px;'>{status_html}{gbif_html}{gbif_fuzzy_html}{wfo_found_html}</div>"

    _key_fields = ("scientificName", "recordedBy", "eventDate", "country",
                   "stateProvince", "locality", "catalogNumber")
    _extraction_failed = not any(
        str(ai_row.get(f, "")).strip()
        for f in _key_fields
        if str(ai_row.get(f, "")).strip().lower() not in ("", "nan", "none", "n/a")
    )

    rows_html   = ""
    cur_section = None

    if _extraction_failed:
        error_msg = str(ai_row.get("error", "")).strip()
        rows_html = f"""
        <tr>
          <td colspan='3' style='padding:24px 16px;'>
            <div style='display:flex; align-items:flex-start; gap:14px;
                background:#fef2f2; border:1.5px solid #fecaca; border-radius:10px; padding:16px 20px;'>
              <div style='font-size:1.4rem; flex-shrink:0;'></div>
              <div>
                <div style='font-size:0.8rem; font-weight:700; color:#b91c1c; margin-bottom:4px;'>
                  Extraction failed — no data returned
                </div>
                <div style='font-size:0.72rem; color:#6b7280; line-height:1.5;'>
                  Gemini returned empty fields for this specimen. This is usually caused by a
                  temporary API error (503 / rate limit) or an unreadable image.
                  {"<br><span style='font-family:monospace;color:#991b1b;'>" + error_msg + "</span>" if error_msg else ""}
                </div>
                <div style='margin-top:10px; font-size:0.68rem; color:#9ca3af;'>
                  Re-run herbaudit on this image alone to retry extraction.
                </div>
              </div>
            </div>
          </td>
        </tr>"""
    else:
        for ai_col, disp_label, section in _NOREF_FIELDS:
            if section != cur_section:
                cur_section = section
                icon = _SECTION_ICON.get(section, "")
                rows_html += (
                    f"<tr><td colspan='3' style='padding:10px 8px 4px; font-size:0.6rem; "
                    f"font-weight:800; letter-spacing:1.5px; color:#9ca3af; border:none; "
                    f"text-transform:uppercase;'>{icon} {section}</td></tr>"
                )
            val = str(ai_row.get(ai_col, "")).strip()
            val_display = (
                "<span style='color:#d1d5db; font-style:italic;'>—</span>"
                if not val or val.lower() in ("nan", "none", "n/a", "")
                else val
            )
            extra = _taxonomy_badge() if ai_col == "scientificName" else ""
            rows_html += f"""
            <tr style='border-bottom:1px solid #f9fafb;'>
              <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#374151; width:24%;'>{disp_label}</td>
              <td style='padding:8px; font-size:0.72rem; font-family:monospace; color:#1f2937; width:46%;'>{val_display}</td>
              <td style='padding:8px; width:30%;'>{extra}</td>
            </tr>"""

    ai_lat = str(ai_row.get("decimalLatitude",  ai_row.get("latitude",  ""))).strip()
    ai_lon = str(ai_row.get("decimalLongitude", ai_row.get("longitude", ""))).strip()

    def _null(v): return not v or v.lower() in ("nan", "none", "n/a", "")

    if not _null(ai_lat) and not _null(ai_lon):
        coord_val  = f"{ai_lat}, {ai_lon}"
        coord_note = ""
    else:
        # Geocode fallback — use whatever location fields are available
        locality = str(ai_row.get("locality",      "")).strip()
        state    = str(ai_row.get("stateProvince", "")).strip()
        country  = str(ai_row.get("country",       "")).strip()
        if not _null(locality) or not _null(state) or not _null(country):
            geo = _geocode_location(locality, state, country)
        else:
            geo = None
        if geo:
            coord_val  = f"{geo[0]}, {geo[1]}"
            coord_note = ("<span style='display:inline-block; margin-left:6px; padding:1px 7px; "
                          "border-radius:99px; font-size:0.6rem; font-weight:700; "
                          "background:#fef9c3; color:#854d0e;'>geocoded</span>")
        else:
            coord_val  = "<span style='color:#d1d5db; font-style:italic;'>—</span>"
            coord_note = ""

    rows_html += f"""
    <tr><td colspan='3' style='padding:10px 8px 4px; font-size:0.6rem; font-weight:800;
        letter-spacing:1.5px; color:#9ca3af; border:none; text-transform:uppercase;'>Coordinates</td></tr>
    <tr style='border-bottom:1px solid #f9fafb;'>
      <td style='padding:8px; font-size:0.72rem; font-weight:600; color:#374151;'>Coordinates</td>
      <td style='padding:8px; font-size:0.72rem; font-family:monospace; color:#1f2937;' colspan='2'>{coord_val}{coord_note}</td>
    </tr>"""

    rows_html += """<tr><td colspan='3' style='padding:10px 8px 4px; font-size:0.6rem;
        font-weight:800; letter-spacing:1.5px; color:#9ca3af; border:none;
        text-transform:uppercase;'>Extraction metadata</td></tr>"""
    for meta_lbl, meta_val in [
        ("Resolution",  res_str),
        ("Model",       model_name or "—"),
        ("Tokens in",   f"<span class='tok-in'>{in_tok_str}</span>"),
        ("Tokens out",  f"<span class='tok-out'>{out_tok_str}</span>"),
        ("Cost",        f"<span class='cost-val'>{cost_str}</span>"),
    ]:
        rows_html += f"""
        <tr style='border-bottom:1px solid #f9fafb; background:#fafafa;'>
          <td style='padding:6px 8px; font-size:0.68rem; font-weight:600; color:#6b7280;'>{meta_lbl}</td>
          <td style='padding:6px 8px; font-size:0.68rem; font-family:monospace; color:#374151;' colspan='2'>{meta_val}</td>
        </tr>"""

    cost_badge = (
        f"<span style='display:inline-block; padding:3px 10px; border-radius:99px; "
        f"font-size:0.65rem; font-weight:700; background:#f0fdf4; color:#16a34a; "
        f"border:1px solid #bbf7d0; margin-left:8px;'>{cost_str}</span>"
        if cost_usd is not None else ""
    )

    data_cost = f"{cost_usd:.8f}" if cost_usd is not None else "0"
    return cost_usd or 0.0, f"""
    <div class='audit-card' data-cost='{data_cost}' style='
        background:#fff; border-radius:16px; margin-bottom:32px;
        box-shadow:0 1px 3px rgba(0,0,0,.07), 0 8px 24px rgba(0,0,0,.06);
        overflow:hidden; transition:box-shadow .2s;'>

      <div style='display:flex; align-items:center; justify-content:space-between;
          padding:18px 24px; border-bottom:1px solid #f3f4f6;
          background:linear-gradient(135deg,#f0fdf4 0%,#fff 100%);'>
        <div>
          <div style='font-size:0.65rem; font-weight:700; letter-spacing:1.5px;
              color:#9ca3af; text-transform:uppercase; margin-bottom:2px;'>Specimen</div>
          <h3 style='margin:0; font-size:1rem; font-weight:700; color:#111827;
              font-family:"IBM Plex Mono",monospace;'>{label}{cost_badge}</h3>
        </div>
        <div style='font-size:0.65rem; color:#9ca3af; text-align:right; font-family:monospace;'>
          {res_str}<br><span style='font-size:0.6rem; color:#d1d5db;'>{model_name}</span>
        </div>
      </div>

      <div style='display:grid; grid-template-columns:1fr 240px;'>
        <div style='padding:0 8px; overflow:auto;'>
          <table style='width:100%; border-collapse:collapse;'>
            <thead>
              <tr style='border-bottom:2px solid #f3f4f6;'>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:24%;'>Field</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:46%;'>AI extraction</th>
                <th style='padding:8px; font-size:0.6rem; font-weight:700; letter-spacing:1px; color:#9ca3af; text-transform:uppercase; text-align:left; width:30%;'>Taxonomy</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        <div style='border-left:1px solid #f3f4f6; padding:16px;
            display:flex; align-items:center; justify-content:center; background:#fafafa;'>
          {img_html}
        </div>
      </div>
    </div>"""
