"""
Compare herbaudit's own already-computed extraction results across one or
more models. This reads the *_audit.json files herbaudit itself writes to
its output directory and plots them — it makes no LLM/API calls, fetches
nothing from GBIF, and computes no scores of its own. All the numbers here
(accuracy, cost, field completeness) are whatever herbaudit already
computed and persisted when it originally ran.

Each *_audit.json is shaped {"darwin_core": {...}, "herbaudit_meta": {...}}
(see herbaudit/functions.py's _split_record) — this script reads
herbaudit_meta directly (falling back to the record itself for older flat
*_audit.json files from before that split existed, same fallback core.py's
own report ingestion uses).

Accuracy per specimen is herbaudit_meta.herbaudit_accuracy (0..1) —
herbaudit's own GBIF-comparison score, patched in by core.py's
_persist_gbif_accuracy after a real audit run found a matching GBIF
occurrence. Genuine correctness. Records with no herbaudit_accuracy (no
GBIF match was found) are skipped entirely — this script never falls back
to field_completeness_pct, which is not a verified accuracy measure.

Since herbaudit writes one <image_stem>_audit.json per image into a single
output directory, and a rerun with a different model overwrites that same
file (see HerbAudit._load_cached_audit's model check in functions.py),
comparing N models means having actually run herbaudit N times into N
separate output directories — one per model. Point this script at all of
them (or at a single folder if you're fine with whatever's mixed in there);
grouping is always by each record's own herbaudit_meta.model field, never
by folder name.

The category/hardness plots need a Gold-Standard.xlsx (columns:
Institution, filename, Catalog Number, Type, GBIF, labelstudio, level)
mapping each sheet to Handwritten/Typed/Mixed plus a hard/not-hard flag —
same file/format herbaudit's model-benchmarking has always used.

Usage:
    # one output directory per model
    python compare_models.py herbaudit_output_gemini-2.5-flash herbaudit_output_gpt-4o \\
        --gold-standard Gold-Standard.xlsx

    # a single folder (fine if it only has one model's results, or if
    # mixed-model files in there should just be grouped by their own
    # herbaudit_meta.model)
    python compare_models.py herbaudit_output --gold-standard Gold-Standard.xlsx
"""
import argparse
import glob
import json
import math
import os
import re
import statistics
import sys

import matplotlib.pyplot as plt
import openpyxl

# Fixed-order, colorblind-validated categorical palette (8 slots) — color
# identifies a model consistently across every plot. Assigned by first-seen
# order, never re-sorted by value, so a model's color doesn't shift between
# runs/panels. Beyond 8 models the 9th+ wrap back to slot 1.
CATEGORICAL_PALETTE = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
MUTED_INK = "#898781"
SECONDARY_INK = "#52514e"
GRIDLINE = "#e1e0d9"

ACCURACY_CATEGORY_COLORS = {
    "Handwritten": "#2a78d6",  # blue
    "Typed":       "#eda100",  # yellow
    "Mixed":       "#1baf7a",  # aqua
}
# Darker shade of each category color, for that category's own mean line —
# needs to read clearly against bars filled with the lighter version above.
MEAN_LINE_COLORS = {
    "Handwritten": "#123e70",
    "Typed":       "#8a5c00",
    "Mixed":       "#0d6b4c",
}
CATEGORIES = ("Handwritten", "Typed", "Mixed")

# herbaudit_meta.model stores the RUNNER name ("ollama"), not the specific
# model it was configured to run — rename to the actual model for display
# everywhere a model name shows up (plots, legends), not just the sketch
# plots, so it stays consistent across the whole script.
MODEL_DISPLAY_NAMES = {
    "ollama": "gemma-4-26B-A4B",
}

# Soft off-white page/chart surfaces (vs. matplotlib's stark white default)
# — same treatment as herbaudit's own bar-chart plots elsewhere.
PAGE_PLANE = "#f9f9f7"
CHART_SURFACE = "#fcfcfb"


def _model_colors(models: list[str]) -> dict[str, str]:
    """First-seen order -> fixed palette slot, so a model keeps the same
    color across every plot."""
    seen = []
    for m in models:
        if m not in seen:
            seen.append(m)
    return {m: CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)] for i, m in enumerate(seen)}


# ── Reading herbaudit's own *_audit.json output ─────────────────────────────

def _iter_audit_json_paths(inputs: list[str]) -> list[str]:
    """Expand each input into concrete *_audit.json file paths — a directory
    is (non-recursively) globbed for *_audit.json, a path already ending in
    .json is used as-is, anything else is skipped with a warning."""
    paths = []
    for p in inputs:
        if os.path.isdir(p):
            found = sorted(glob.glob(os.path.join(p, "*_audit.json")))
            if not found:
                print(f"WARNING: no *_audit.json files found in {p}")
            paths.extend(found)
        elif p.endswith(".json"):
            paths.append(p)
        else:
            print(f"WARNING: skipping {p} — not a directory or a .json file")
    return paths


def _load_records(inputs: list[str]) -> list[dict]:
    """Read every *_audit.json under the given paths — no API calls, no
    scoring, just pulling out what herbaudit itself already computed."""
    records = []
    n_error = n_bad = n_no_accuracy = 0
    for path in _iter_audit_json_paths(inputs):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            n_bad += 1
            continue
        if "error" in data:
            n_error += 1
            continue

        meta = data.get("herbaudit_meta", data)   # fall back to legacy flat files
        model = meta.get("model") or "unknown"
        model = MODEL_DISPLAY_NAMES.get(model, model)
        image = meta.get("source_image") or os.path.basename(path)[: -len("_audit.json")]

        herbaudit_accuracy = meta.get("herbaudit_accuracy")        # 0..1, GBIF-scored, or None
        if herbaudit_accuracy is None:
            n_no_accuracy += 1
            continue
        accuracy_pct = herbaudit_accuracy * 100

        # The ORIGINAL source photo's resolution — precrop_width/height,
        # before any label detection/cropping — not image_width/image_height
        # (which is the cropped/collaged image actually sent to the model,
        # and conflates "higher resolution" with "the detector found more/
        # bigger label regions to crop out", since crops are packed at
        # native size with no resize cap; see herbaudit/functions.py's
        # _build_collage_bytes). This is what actually answers "does a
        # higher-res photo help accuracy" without that confound.
        photo_width  = meta.get("precrop_width")
        photo_height = meta.get("precrop_height")
        pixel_count = (photo_width * photo_height
                       if photo_width and photo_height else None)

        # What actually reached the model, AFTER label detection/cropping —
        # the complement to photo_width/height above: this is the resolution
        # of the crop/collage itself, not the original uploaded photo.
        crop_width  = meta.get("image_width")
        crop_height = meta.get("image_height")
        crop_pixel_count = (crop_width * crop_height
                            if crop_width and crop_height else None)

        records.append({
            "model":            model,
            "image":            image,
            "cost_usd":         meta.get("cost_usd") or 0.0,
            "accuracy_pct":     accuracy_pct,
            "photo_width":      photo_width,
            "photo_height":     photo_height,
            "pixel_count":      pixel_count,
            "crop_width":       crop_width,
            "crop_height":      crop_height,
            "crop_pixel_count": crop_pixel_count,
            "collage_used":     meta.get("collage_used"),
            "detector_used":    meta.get("detector_used"),
            "_path":            path,
        })

    if n_error:
        print(f"Skipped {n_error} file(s) with an 'error' key (failed extraction).")
    if n_bad:
        print(f"Skipped {n_bad} file(s) that couldn't be parsed as JSON.")
    if n_no_accuracy:
        print(f"Skipped {n_no_accuracy} file(s) with no herbaudit_accuracy set "
              f"(no GBIF match found).")
    return records


# ── Gold Standard category/hardness lookup ──────────────────────────────────

def _load_gold_standard(path: str) -> dict[str, dict]:
    """Reads the Gold-Standard.xlsx specimen categorization (columns:
    Institution, filename, Catalog Number, Type, GBIF, labelstudio, level)
    and returns {match_key: {"category": ..., "hard": bool}}.

    match_key is whichever of the row's GBIF id, catalog number, or its own
    filename column actually matches an image filename stem in this run.
    GBIF id is what herbaudit itself names most sheet files after; catalog
    number covers the local (e.g. GENT) sheets with no GBIF occurrence — but
    neither is universal: some rows' real identifying stem only lives in the
    sheet's own filename column (e.g. an institution-specific rename that
    doesn't match either the GBIF id or the catalog number), so all three are
    indexed as match keys, not just GBIF/catalog number.

    "Type" free text (e.g. "Handwritten/Typed/Typus", "Typed (Hard) ") is
    collapsed to one of Handwritten/Typed/Mixed by checking which of the
    words "Handwritten"/"Typed" appear — "Typus" is a taxonomic nomenclature
    marker, unrelated to handwriting vs. print, so it's ignored for
    categorization. "hard" is true if the level column says so OR the Type
    text itself says "(Hard)" (seen on one row with no level value set).
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    out = {}
    for institution, filename, catalog_number, type_text, gbif, labelstudio, level in rows:
        type_text = (type_text or "")
        has_hw = "handwritten" in type_text.lower()
        has_typed = "typed" in type_text.lower()
        if has_hw and has_typed:
            category = "Mixed"
        elif has_hw:
            category = "Handwritten"
        elif has_typed:
            category = "Typed"
        else:
            continue  # unrecognized Type text — skip rather than guess
        hard = (level == "hard") or ("(hard)" in type_text.lower())

        cat_key = str(catalog_number).replace("\t", "").replace("\xa0", "").strip() if catalog_number is not None else ""
        gbif_key = str(gbif) if gbif is not None else None
        # The row's own filename column, stripped to a bare stem the same way
        # _match_gold_standard_rows strips the image filename it's matching
        # against — some rows' real identifying stem lives only here, not in
        # GBIF id or catalog number (e.g. an institution-specific rename).
        fn_key = (os.path.splitext(str(filename))[0].replace("\t", "").replace("\xa0", "").strip()
                  if filename is not None else "")
        entry = {"category": category, "hard": hard}
        if gbif_key:
            out[gbif_key] = entry
        if cat_key:
            out[cat_key] = entry
        if fn_key:
            out[fn_key] = entry
    return out


def _match_gold_standard_rows(records: list[dict], gold_standard_path: str) -> list[dict]:
    """Records whose image matches a Gold Standard row, each tagged with
    "_category" (Handwritten/Typed/Mixed) and "_hard" (bool). Records with
    no match are dropped (and reported)."""
    gold = _load_gold_standard(gold_standard_path)
    unmatched = set()
    matched = []
    for r in records:
        stem = os.path.splitext(r["image"])[0]
        entry = gold.get(stem)
        if entry is None:
            unmatched.add(r["image"])
        else:
            matched.append({**r, "_category": entry["category"], "_hard": entry["hard"]})
    if unmatched:
        print(f"Note: {len(unmatched)} image(s) have no Gold Standard match, excluded from "
              f"the category plots: {', '.join(sorted(unmatched))}")
    return matched


def _accuracy_label(matched: list[dict]) -> str:
    """Y-axis label for herbaudit's own GBIF-verified accuracy score."""
    return "HerbAudit Accuracy (%)"


# ── shared renderer: 3 stacked category panels (Handwritten/Typed/Mixed) ────
# per model PNG — used by both the accuracy-sorted and resolution-sorted
# by-category plots below, which are identical in structure and differ only
# in sort order, bar color, and x-tick label content.

def _render_by_category_panels(
    matched: list[dict], out_prefix: str, *,
    file_tag: str, subtitle_note: str,
    sort_key, sort_reverse: bool,
    bar_color_fn, tick_label_fn,
) -> None:
    if not matched:
        print(f"No Gold-Standard-matched sheets — skipping {file_tag} plot.")
        return

    y_label = _accuracy_label(matched)

    by_sheet: dict[str, list[dict]] = {}
    for r in matched:
        by_sheet.setdefault(r["image"], []).append(r)

    sheets_by_category = {
        cat: [img for img, rows in by_sheet.items() if rows[0]["_category"] == cat]
        for cat in CATEGORIES
    }

    models_order = []
    for r in matched:
        if r["model"] not in models_order:
            models_order.append(r["model"])

    model_mean = {}
    for model in models_order:
        vals = [r["accuracy_pct"] for rows in by_sheet.values() for r in rows if r["model"] == model]
        model_mean[model] = sum(vals) / len(vals) if vals else 0.0
    models_order.sort(key=lambda m: model_mean[m], reverse=True)

    hard_handle = plt.Rectangle((0, 0), 1, 1, facecolor="0.6", hatch="///",
                                  edgecolor="#2b2b2b", linewidth=0.4, label="Hard")
    trend_handle = plt.Line2D([0], [0], color="#d62728", linewidth=1.4, marker="o",
                               markersize=2.5, alpha=0.55, label="Sheet trend")
    mean_handle = plt.Line2D([0], [0], color=SECONDARY_INK, linewidth=1.8,
                              linestyle="--", label="Category mean")

    for rank, model in enumerate(models_order, start=1):
        def row_of(img: str, model=model) -> dict | None:
            return next((r for r in by_sheet[img] if r["model"] == model), None)

        def sort_val(img: str, model=model) -> float:
            row = row_of(img, model=model)
            return sort_key(row) if row is not None else 0.0

        blocks = {cat: sorted(sheets_by_category[cat], key=sort_val, reverse=sort_reverse)
                  for cat in CATEGORIES}
        max_block_len = max((len(b) for b in blocks.values()), default=0)
        if max_block_len == 0:
            continue
        fig_width = max(16, max_block_len * 0.4)
        fig, axes = plt.subplots(len(CATEGORIES), 1, figsize=(fig_width, 5.5 * len(CATEGORIES)))
        fig.patch.set_facecolor(PAGE_PLANE)

        for ax, cat in zip(axes, CATEGORIES):
            ax.set_facecolor(CHART_SURFACE)
            block = blocks[cat]
            rows = [row_of(img, model=model) for img in block]
            positions = list(range(len(block)))
            ys = [r["accuracy_pct"] for r in rows]
            hard_flags = [r["_hard"] for r in rows]
            cat_mean = sum(ys) / len(ys) if ys else 0.0

            colors = [bar_color_fn(r, cat) for r in rows]
            bars = ax.bar(positions, ys, color=colors, width=0.72,
                          edgecolor="white", linewidth=0.6, zorder=3)
            for bar_patch, is_hard in zip(bars, hard_flags):
                if is_hard:
                    bar_patch.set_hatch("///")
                    bar_patch.set_edgecolor("#2b2b2b")
                    bar_patch.set_linewidth(0.5)

            if positions:
                ax.plot(positions, ys, color="#d62728", linewidth=1.4,
                        marker="o", markersize=2.5, alpha=0.55, zorder=4)

            # Category mean — the headline number this panel adds beyond the
            # raw per-sheet bars, so it gets its own dashed line + callout
            # rather than making a reader eyeball the bar heights for it.
            ax.axhline(cat_mean, color=MEAN_LINE_COLORS[cat], linestyle="--",
                       linewidth=1.8, zorder=6)
            ax.annotate(
                f"Mean {cat_mean:.1f}%",
                (0.992, cat_mean), xycoords=("axes fraction", "data"),
                xytext=(0, 9), textcoords="offset points",
                ha="right", va="bottom", fontsize=10, fontweight="bold",
                color="white",
                bbox=dict(boxstyle="round,pad=0.3", fc=MEAN_LINE_COLORS[cat], ec="none"),
                zorder=7,
            )

            ax.set_xticks(positions)
            ax.set_xticklabels([tick_label_fn(r) for r in rows],
                                rotation=90, fontsize=6, color=SECONDARY_INK)
            ax.tick_params(axis="x", length=2, pad=2)
            ax.set_xlim(-0.7, max(len(block), 1) - 0.3)
            ax.set_ylim(0, 112)
            ax.set_ylabel(y_label, fontsize=9, color=SECONDARY_INK)
            ax.set_title(f"{cat}  ·  n={len(block)}", fontsize=12, fontweight="bold",
                        loc="left", color="#0b0b0b")
            ax.grid(True, axis="y", color=GRIDLINE, alpha=0.7, linewidth=0.8, zorder=0)
            ax.set_axisbelow(True)
            ax.tick_params(axis="y", colors=MUTED_INK, labelsize=8)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(GRIDLINE)

        # Reserve a generous top margin first, THEN place the header text at
        # fixed y positions within it — doing it in this order (rather than
        # placing text first and letting tight_layout guess its extent)
        # avoids the title/subtitle colliding, since tight_layout has no idea
        # how tall arbitrary fig.text()/suptitle() calls actually render.
        plt.tight_layout(rect=(0, 0, 1, 0.90))
        fig.suptitle(f"{model}", fontsize=18, fontweight="bold",
                     x=0.01, y=0.985, ha="left", va="top", color="#0b0b0b")
        fig.text(0.01, 0.945, f"Overall mean {model_mean[model]:.1f}% across "
                 f"{sum(len(b) for b in sheets_by_category.values())} matched sheets"
                 f"{subtitle_note}",
                 fontsize=11, color=SECONDARY_INK, ha="left", va="top")
        fig.legend(handles=[hard_handle, trend_handle, mean_handle], loc="upper right",
                   bbox_to_anchor=(0.99, 0.985), ncol=3, fontsize=9.5, frameon=False)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
        plot_path = f"{out_prefix}_{file_tag}_{rank:02d}_{safe_name}.png"
        plt.savefig(plot_path, dpi=150, facecolor=PAGE_PLANE)
        plt.close(fig)
        print(f"Saved {plot_path}")


# ── PLOT 1: per-model, per-category, per-sheet accuracy (hard hatched) ──────

def _plot_accuracy_by_category(matched: list[dict], out_prefix: str) -> None:
    """One PNG per model — 3 stacked panels (Handwritten/Typed/Mixed), each
    bar a sheet's accuracy for that model, sorted best-to-worst within its
    category block, hard sheets cross-hatched. Files are ranked in the
    filename by each model's own mean accuracy, best first.
    """
    _render_by_category_panels(
        matched, out_prefix,
        file_tag="by_category", subtitle_note="",
        sort_key=lambda r: r["accuracy_pct"], sort_reverse=True,
        bar_color_fn=lambda r, cat: ACCURACY_CATEGORY_COLORS[cat],
        tick_label_fn=lambda r: os.path.splitext(r["image"])[0],
    )


# ── PLOT: accuracy distribution per category, all models pooled ────────────

BOX_COLORS = {
    "Handwritten": "#2a86c4",  # blue
    "Typed":       "#d9731a",  # orange
    "Mixed":       "#1bab85",  # green
}
OUTLIER_STAR_COLOR = "#3aa6e0"


def _plot_accuracy_boxplot_by_category(matched: list[dict], out_prefix: str) -> None:
    """One PNG, all models combined (each sheet's accuracy is averaged
    across models, so n = sheets): per category (Handwritten/Typed/Mixed) a
    jittered point per record on the left and a box on the right (solid
    median, dashed mean, 1.5*IQR whiskers). Points beyond the whiskers get a
    star. X labels carry n and mean."""
    import numpy as np

    if not matched:
        print("No Gold-Standard-matched sheets — skipping boxplot.")
        return

    rng = np.random.default_rng(0)
    fig, ax = plt.subplots(figsize=(12.8, 8.8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    tick_labels = []
    for i, cat in enumerate(CATEGORIES):
        per_sheet: dict[str, list[float]] = {}
        for r in matched:
            if r["_category"] == cat:
                per_sheet.setdefault(r["image"], []).append(r["accuracy_pct"])
        vals = np.array([sum(v) / len(v) for v in per_sheet.values()], dtype=float)
        center = float(i)
        if not len(vals):
            tick_labels.append(f"{cat}\n(n=0)")
            continue
        tick_labels.append(f"{cat}\n(n={len(vals)}, mean {vals.mean():.1f}%)")
        color = BOX_COLORS[cat]
        box_x = center + 0.18
        pt_x = center - 0.28 + rng.uniform(-0.1, 0.1, len(vals))

        q1, q3 = np.percentile(vals, [25, 75])
        iqr = q3 - q1
        lo_w = vals[vals >= q1 - 1.5 * iqr].min()
        hi_w = vals[vals <= q3 + 1.5 * iqr].max()

        ax.boxplot(vals, positions=[box_x], widths=0.36, whis=1.5, showfliers=False,
                   patch_artist=True, manage_ticks=False,
                   boxprops=dict(facecolor=color, alpha=0.6, edgecolor=color, linewidth=1.2),
                   medianprops=dict(color="black", linewidth=2.2),
                   whiskerprops=dict(color="black", linewidth=1.3),
                   capprops=dict(color="black", linewidth=1.3))
        ax.hlines(vals.mean(), box_x - 0.18, box_x + 0.18, color="black",
                  linestyle="--", linewidth=2.0, zorder=4)

        ax.scatter(pt_x, vals, s=48, color=color, edgecolor="white", linewidth=0.8,
                   alpha=0.95, zorder=5)
        out = (vals < lo_w) | (vals > hi_w)
        ax.scatter(pt_x[out] + 0.07, vals[out], marker="*", s=46, color=OUTLIER_STAR_COLOR,
                   zorder=6)

    ax.set_xticks(range(len(CATEGORIES)))
    ax.set_xticklabels(tick_labels, fontsize=13, fontweight="bold", color="#222222")
    ax.set_xlim(-0.6, len(CATEGORIES) - 0.4)
    ax.set_ylim(59, 105)
    ax.set_yticks(range(60, 101, 10))
    ax.set_ylabel(_accuracy_label(matched), fontsize=12, color=SECONDARY_INK)
    ax.grid(True, axis="y", color=GRIDLINE, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", colors=MUTED_INK, labelsize=11)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRIDLINE)
    ax.legend(handles=[plt.Line2D([0], [0], color="black", linewidth=2.2, label="Median"),
                       plt.Line2D([0], [0], color="black", linewidth=2.0, linestyle="--", label="Mean")],
              loc="lower left", frameon=False, fontsize=11)

    plt.tight_layout()
    plot_path = f"{out_prefix}_accuracy_boxplot.png"
    plt.savefig(plot_path, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"Saved {plot_path}")


# ── PLOT 2: mean accuracy per category (+ Overall), grouped by model ───────

def _plot_mean_accuracy_by_category(matched: list[dict], out_prefix: str) -> None:
    """One grouped-bar chart: category (Handwritten/Typed/Mixed/Overall) on
    the x-axis, one bar per model within each group — the mean accuracy
    across that category's matched sheets for that model. A hollow diamond
    marker overlays the hard-sheets-only mean wherever a model has any hard
    sheets in that group, so the "hard" difficulty flag stays visible
    without doubling the bar count.
    """
    if not matched:
        print("No Gold-Standard-matched sheets — skipping mean-by-category plot.")
        return

    y_label = _accuracy_label(matched)

    models_order = []
    for r in matched:
        if r["model"] not in models_order:
            models_order.append(r["model"])
    model_color = _model_colors(models_order)

    groups = list(CATEGORIES) + ["Overall"]

    def _mean(rows):
        return sum(r["accuracy_pct"] for r in rows) / len(rows) if rows else None

    # means[group][model] = (mean_over_all, mean_over_hard_only_or_None, n)
    means: dict[str, dict[str, tuple]] = {}
    for group in groups:
        means[group] = {}
        for model in models_order:
            rows = [r for r in matched if r["model"] == model
                    and (group == "Overall" or r["_category"] == group)]
            if not rows:
                continue
            hard_rows = [r for r in rows if r["_hard"]]
            means[group][model] = (_mean(rows), _mean(hard_rows) if hard_rows else None, len(rows))

    # Rank models by their Overall mean, best first — consistent bar order
    # across every group.
    models_order.sort(key=lambda m: (means["Overall"].get(m) or (0.0,))[0], reverse=True)

    n_models = max(len(models_order), 1)
    group_width = 0.8
    bar_width = group_width / n_models

    fig, ax = plt.subplots(figsize=(max(9, 2.0 * len(groups) * n_models / 3), 6.5))

    for mi, model in enumerate(models_order):
        xs, ys = [], []
        hard_xs, hard_ys = [], []
        for gi, group in enumerate(groups):
            entry = means[group].get(model)
            x = gi - group_width / 2 + bar_width * (mi + 0.5)
            xs.append(x)
            ys.append(entry[0] if entry else 0.0)
            if entry and entry[1] is not None:
                hard_xs.append(x)
                hard_ys.append(entry[1])

        bars = ax.bar(xs, ys, width=bar_width * 0.92, color=model_color[model], zorder=3, label=model)
        for rect, y, group in zip(bars, ys, groups):
            if means[group].get(model) is None:
                continue
            ax.annotate(f"{y:.1f}%", (rect.get_x() + rect.get_width() / 2, y),
                        textcoords="offset points", xytext=(0, 3),
                        ha="center", va="bottom", fontsize=7, color=SECONDARY_INK)
        if hard_xs:
            ax.scatter(hard_xs, hard_ys, marker="D", s=36, facecolor="white",
                       edgecolor="#2b2b2b", linewidth=1.1, zorder=5)

    ax.set_xticks(list(range(len(groups))))
    ax.set_xticklabels(groups, fontsize=11, fontweight="bold")
    ax.set_ylim(0, 112)
    ax.set_ylabel(y_label, fontsize=9)
    ax.set_title("Mean accuracy by specimen category", fontsize=14, fontweight="bold")
    ax.grid(True, axis="y", alpha=0.3, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    hard_handle = plt.Line2D([0], [0], marker="D", color="none", markerfacecolor="white",
                              markeredgecolor="#2b2b2b", markersize=7, label="Hard-only mean")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles=handles + [hard_handle], loc="upper center",
              bbox_to_anchor=(0.5, -0.12), ncol=min(n_models + 1, 5), fontsize=9, frameon=False)

    plt.tight_layout()
    plot_path = f"{out_prefix}_mean_by_category.png"
    plt.savefig(plot_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {plot_path}")


# Single-hue light->dark, used to shade each sheet's bar by its own
# resolution (darker = higher-resolution) — reinforces the left-to-right
# sort without needing a second y-axis (this script never does dual-axis
# charts — see choosing-a-form guidance).
RESOLUTION_SHADE_LIGHT = (0xd6, 0xe7, 0xfa)
RESOLUTION_SHADE_DARK  = (0x12, 0x3e, 0x70)


def _sequential_shade(t: float) -> str:
    t = max(0.0, min(1.0, t))
    rgb = tuple(round(a + (b - a) * t) for a, b in zip(RESOLUTION_SHADE_LIGHT, RESOLUTION_SHADE_DARK))
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ── PLOT 3: accuracy vs. ORIGINAL PHOTO resolution, per sheet, by category ──

def _plot_resolution_accuracy(matched: list[dict], out_prefix: str) -> None:
    """Same 3-panel-by-category layout as _plot_accuracy_by_category, but
    sheets are sorted left-to-right by their original source photo's
    resolution (precrop_width x precrop_height, in pixels — the photo AS
    UPLOADED, before any label detection/cropping) instead of by accuracy,
    so any resolution/accuracy relationship is visible directly. This is
    deliberately NOT image_width/image_height (the cropped/collaged image
    actually sent to the model) — that number conflates "higher resolution"
    with "the detector found more/bigger label regions to crop out", since
    crops get packed at native size with no resize cap (see
    herbaudit/functions.py's _build_collage_bytes). precrop_width/height is
    upstream of all that: purely how high-res the photo itself was.

    Each bar also darkens with resolution (redundant with the sort, but
    makes "this one was a higher-res photo" readable without a second
    axis). X-axis labels combine the sheet's own name (its GBIF ID /
    filename stem — the Gold Standard match key) with its actual photo
    width x height IN PIXELS, not megapixels (a less intuitive unit).

    A raw scatter, then a quantile-binned mean, were tried first and
    dropped: the scatter was ~90 overlapping dots mostly pinned at 100%
    accuracy plus a barely-visible trend line, and the binned version hid
    the sheet-level detail this is meant to show. Requires a Gold Standard
    match (unlike the first version of this plot) since category is now the
    grouping dimension — records with no match are already excluded from
    `matched` by _match_gold_standard_rows.
    """
    usable = [r for r in matched if r["pixel_count"] is not None]
    skipped = len(matched) - len(usable)
    if skipped:
        print(f"Note: {skipped} record(s) have no precrop_width/precrop_height "
              f"(older *_audit.json?), excluded from the resolution/accuracy plot.")
    if not usable:
        print("No records with a source photo resolution — skipping resolution/accuracy plot.")
        return

    px_values = [r["pixel_count"] for r in usable]
    px_lo, px_hi = min(px_values), max(px_values)
    px_span = (px_hi - px_lo) or 1

    def tick_label(r: dict) -> str:
        stem = os.path.splitext(r["image"])[0]
        return f'{stem}\n{r["photo_width"]}×{r["photo_height"]}'

    _render_by_category_panels(
        usable, out_prefix,
        file_tag="resolution_by_category",
        subtitle_note="  ·  sorted by original photo resolution, low→high "
                      "(darker bar = higher resolution)",
        sort_key=lambda r: r["pixel_count"], sort_reverse=False,
        bar_color_fn=lambda r, cat: _sequential_shade((r["pixel_count"] - px_lo) / px_span),
        tick_label_fn=tick_label,
    )


# ── PLOT 4: accuracy vs. CROP/COLLAGE resolution actually sent to the model ─

def _plot_crop_resolution_accuracy(matched: list[dict], out_prefix: str) -> None:
    """Same layout as _plot_resolution_accuracy, but sorted by the
    crop/collage resolution actually sent to the model (herbaudit_meta.
    image_width x image_height) — the complementary check: the original
    photo's own resolution turned out not to predict accuracy, so the next
    thing worth checking is whether how much survived label detection and
    cropping does. This number reflects two things at once (see
    herbaudit/functions.py's _build_collage_bytes): usually how many/how
    large the detected label regions were (crops are packed at native size,
    no resize cap), or — for sheets where detection failed and it fell back
    to sending the whole photo (collage_used == False) — a straightforward
    downsize to max_resolution. Each x-tick label notes which case it was
    ("crop" vs "full") so that distinction stays visible rather than hidden
    behind a single resolution number.
    """
    usable = [r for r in matched if r["crop_pixel_count"] is not None]
    skipped = len(matched) - len(usable)
    if skipped:
        print(f"Note: {skipped} record(s) have no image_width/image_height "
              f"(older *_audit.json?), excluded from the crop-resolution plot.")
    if not usable:
        print("No records with a sent-image resolution — skipping crop-resolution plot.")
        return

    px_values = [r["crop_pixel_count"] for r in usable]
    px_lo, px_hi = min(px_values), max(px_values)
    px_span = (px_hi - px_lo) or 1

    n_full = sum(1 for r in usable if not r["collage_used"])
    if n_full:
        print(f"  Note: {n_full}/{len(usable)} sheet(s) had no successful crop — the full "
              f"photo (resized) was sent instead. Their x-tick label says 'full' rather "
              f"than 'crop'.")

    def tick_label(r: dict) -> str:
        stem = os.path.splitext(r["image"])[0]
        kind = "crop" if r["collage_used"] else "full"
        return f'{stem}\n{r["crop_width"]}×{r["crop_height"]} ({kind})'

    _render_by_category_panels(
        usable, out_prefix,
        file_tag="crop_resolution_by_category",
        subtitle_note="  ·  sorted by resolution actually sent to the model, low→high "
                      "(darker bar = higher resolution; 'full' = detection found no "
                      "crop, whole photo sent instead)",
        sort_key=lambda r: r["crop_pixel_count"], sort_reverse=False,
        bar_color_fn=lambda r, cat: _sequential_shade((r["crop_pixel_count"] - px_lo) / px_span),
        tick_label_fn=tick_label,
    )


# ── PLOT 5: accuracy vs. cost (colorbar) and time, one bar per model ────────

def _load_times(path: str | None) -> dict[str, dict]:
    """Reads run_multi_model.py's <out-prefix>_times.json manifest —
    {model: {wall_seconds, n_images, seconds_per_image, output_dir}}.
    herbaudit itself never persists timing into *_audit.json (only cost and
    accuracy are), so this is the only source of per-model time. Returns {}
    (rather than raising) when no --times path was given at all — the plot
    below still renders, just without time annotations."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    # Same rename as _load_records — the manifest is keyed by the runner
    # name ("ollama"), which must match records' now-renamed model name or
    # every times.get(model) lookup downstream silently misses.
    return {MODEL_DISPLAY_NAMES.get(k, k): v for k, v in raw.items()}


def _plot_cost_accuracy_time(records: list[dict], out_prefix: str, times: dict[str, dict]) -> None:
    """One bar per model, no Gold Standard match required (unlike the
    by-category plots — this is a model-level aggregate, not per-sheet):
    height = mean accuracy, color = mean cost/image via a continuous
    colormap + colorbar (the one plot in this script NOT using the fixed
    categorical/single-hue palette, since cost is its own independent
    continuous variable here, not a model identity or a resolution sort).
    Bars are ordered left-to-right by cost, low to high. Time is annotated
    as text on each bar rather than a second y-axis — this script never
    does dual-axis charts (see _plot_resolution_accuracy's shading
    approach for the same reasoning applied to resolution).
    """
    if not records:
        print("No records — skipping cost/accuracy/time plot.")
        return

    models_order = []
    for r in records:
        if r["model"] not in models_order:
            models_order.append(r["model"])

    stats = {}
    for model in models_order:
        rows = [r for r in records if r["model"] == model]
        t = times.get(model, {})
        stats[model] = {
            "accuracy":    sum(r["accuracy_pct"] for r in rows) / len(rows),
            "cost":        sum(r["cost_usd"] for r in rows) / len(rows),
            "n":           len(rows),
            "sec_per_img": t.get("seconds_per_image"),
            "wall":        t.get("wall_seconds"),
        }
    models_order.sort(key=lambda m: stats[m]["cost"])

    costs = [stats[m]["cost"] for m in models_order]
    cost_lo, cost_hi = min(costs), max(costs)
    cost_span = (cost_hi - cost_lo) or 1e-9

    cmap = plt.get_cmap("viridis")
    colors = [cmap((c - cost_lo) / cost_span) for c in costs]

    fig, ax = plt.subplots(figsize=(max(8, 1.6 * len(models_order)), 6.5))
    fig.patch.set_facecolor(PAGE_PLANE)
    ax.set_facecolor(CHART_SURFACE)

    xs = list(range(len(models_order)))
    ys = [stats[m]["accuracy"] for m in models_order]
    bars = ax.bar(xs, ys, color=colors, width=0.62, edgecolor="white",
                   linewidth=0.8, zorder=3)

    for model, bar in zip(models_order, bars):
        s = stats[model]
        lines = [f"{s['accuracy']:.1f}%", f"${s['cost']:.4f}/img"]
        if s["sec_per_img"] is not None:
            lines.append(f"{s['sec_per_img']:.1f}s/img")
        ax.annotate("\n".join(lines),
                    (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    textcoords="offset points", xytext=(0, 5),
                    ha="center", va="bottom", fontsize=8.5, color=SECONDARY_INK,
                    linespacing=1.4, zorder=5)

    ax.set_xticks(xs)
    ax.set_xticklabels(models_order, rotation=20, ha="right", fontsize=10)
    ax.set_ylim(0, 122)
    ax.set_ylabel("Mean HerbAudit Accuracy (%)", fontsize=10, color=SECONDARY_INK)
    ax.set_title("Accuracy vs. cost (low→high) and time, per model", fontsize=14,
                 fontweight="bold", loc="left", color="#0b0b0b")
    ax.grid(True, axis="y", color=GRIDLINE, alpha=0.7, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRIDLINE)

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=cost_lo, vmax=cost_hi))
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, pad=0.02)
    cbar.set_label("Mean cost per image ($)", fontsize=9, color=SECONDARY_INK)

    missing_time = [m for m in models_order if stats[m]["sec_per_img"] is None]
    if missing_time:
        note = ("no --times given" if not times else
                 f"no timing entry for: {', '.join(missing_time)}")
        fig.text(0.01, 0.01, f"Note: time not shown for some/all bars ({note}) — pass "
                  f"--times <out-prefix>_times.json from run_multi_model.py.",
                  fontsize=8, color=MUTED_INK)

    plt.tight_layout()
    plot_path = f"{out_prefix}_cost_accuracy_time.png"
    plt.savefig(plot_path, dpi=150, facecolor=PAGE_PLANE, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {plot_path}")


# ── PLOT 6: three independent panels — Accuracy / Cost / Time per model ─────

def _plot_metrics_dashboard(records: list[dict], out_prefix: str, times: dict[str, dict]) -> None:
    """
    One figure, three bar panels side by side — Accuracy, Cost, Time — one
    bar per model in each. Each panel is sorted low->high by ITS OWN
    metric, so the model order can (and usually will) differ between
    panels — deliberately not the shared "everyone sorted by cost" scheme
    _plot_cost_accuracy_time above uses; that plot exists to trace one
    model's trade-off at a fixed x-position, this one is three independent
    low->high rankings instead. Every model keeps the same color across
    all three panels (via _model_colors' fixed first-seen-order palette),
    so it's still recognizable panel to panel even though its x-position
    moves.

    Accuracy and cost get error bars (std dev across that model's own
    matched sheets) since both vary sheet-to-sheet; time doesn't (only a
    per-run aggregate exists — see _load_times' docstring) so its panel
    shows a plain bar. Every bar is annotated with its exact value and
    sample size, since bar height alone is hard to read precisely.
    """
    if not records:
        print("No records — skipping metrics dashboard.")
        return

    models_order = []
    for r in records:
        if r["model"] not in models_order:
            models_order.append(r["model"])
    model_color = _model_colors(models_order)

    stats = {}
    for model in models_order:
        rows = [r for r in records if r["model"] == model]
        accs = [r["accuracy_pct"] for r in rows]
        costs = [r["cost_usd"] for r in rows]
        t = times.get(model, {})
        cost_mean = sum(costs) / len(costs)
        stats[model] = {
            "acc_mean":    sum(accs) / len(accs),
            "acc_std":     statistics.stdev(accs) if len(accs) > 1 else 0.0,
            "cost_mean":   cost_mean,
            "cost_std":    statistics.stdev(costs) if len(costs) > 1 else 0.0,
            # Projected $ cost to run 1000 specimens through this model —
            # straight multiplication of the mean per-specimen cost, since
            # that's the number people actually plan a batch budget against
            # ("what would 1000 sheets cost me") rather than a
            # fraction-of-a-cent-per-image number that's hard to reason
            # about at a glance. $0 for a free/local model (Ollama) is a
            # perfectly normal value here — no divide-by-zero case to
            # special-case, unlike the inverted "specimens per $1000" framing.
            "cost_per_1000": cost_mean * 1000,
            "sec_per_img": t.get("seconds_per_image"),
            "n":           len(rows),
        }

    def _bar_panel(ax, mean_key, std_key, ylabel, title, fmt, y_top_pad):
        ordered = sorted(models_order,
                          key=lambda m: (stats[m][mean_key] is None, stats[m][mean_key] or 0.0))
        ys    = [stats[m][mean_key] or 0.0 for m in ordered]
        yerr  = [stats[m][std_key] for m in ordered] if std_key else None
        colors = [model_color[m] for m in ordered]
        xs = list(range(len(ordered)))

        ax.set_facecolor(CHART_SURFACE)
        ax.bar(xs, ys, color=colors, width=0.62, edgecolor="white", linewidth=0.9,
               zorder=3, yerr=yerr, ecolor=SECONDARY_INK, capsize=3,
               error_kw={"linewidth": 1, "zorder": 4} if yerr else {})

        max_extent = max((y + (e or 0) for y, e in zip(ys, yerr or [0] * len(ys))), default=1) or 1
        for x, model in zip(xs, ordered):
            s = stats[model]
            val = s[mean_key]
            top = (val or 0.0) + (s[std_key] if std_key else 0.0)
            label = fmt.format(val) if val is not None else "n/a"
            ax.annotate(label, (x, top), xytext=(0, 4), textcoords="offset points",
                        ha="center", va="bottom", fontsize=7.5, fontweight="bold",
                        color=SECONDARY_INK, zorder=5)
            ax.annotate(f"n={s['n']}", (x, 0), xytext=(0, -18), textcoords="offset points",
                        ha="center", va="top", fontsize=6.5, color=MUTED_INK)

        ax.set_xticks(xs)
        ax.set_xticklabels(ordered, rotation=30, ha="right", fontsize=7.5)
        ax.set_ylabel(ylabel, fontsize=8.5, color=SECONDARY_INK)
        ax.set_title(title, fontsize=12, fontweight="bold", loc="left", color="#0b0b0b")
        ax.set_ylim(0, max_extent * y_top_pad)
        ax.grid(True, axis="y", color=GRIDLINE, alpha=0.7, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRIDLINE)
        ax.tick_params(axis="y", colors=MUTED_INK, labelsize=7.5)

    # Stacked (3 rows, not 3 side-by-side columns) and sized to a single US
    # Letter page in portrait — the old 1x3 layout scaled its width with the
    # model count (3.4in x 12 models = ~41in wide) and could never fit on a
    # printed page regardless of DPI. Each panel now gets the full page
    # width for its 12 x-axis labels instead of a third of it.
    fig, axes = plt.subplots(3, 1, figsize=(8.5, 11))
    fig.patch.set_facecolor(PAGE_PLANE)

    _bar_panel(axes[0], "acc_mean", "acc_std", "Mean HerbAudit Accuracy (%)",
               "Accuracy — worst → best", "{:.1f}%", 1.20)
    _bar_panel(axes[1], "cost_per_1000", None, "Cost per 1000 specimens ($)",
               "Cost — cheapest → priciest per 1000 specimens", "${:.1f}", 1.28)
    _bar_panel(axes[2], "sec_per_img", None, "Seconds per image",
               "Time — fastest → slowest", "{:.1f}s", 1.20)

    handles = [plt.Rectangle((0, 0), 1, 1, facecolor=model_color[m], label=m)
               for m in models_order]
    # ncol chosen so the legend wraps to at most 3 rows regardless of model
    # count, and tight_layout's rect below reserves enough fixed top margin
    # for suptitle + subtitle + this legend so it can never overlap the
    # first panel's own title (what was happening before this margin existed).
    legend_ncol = max(3, math.ceil(len(models_order) / 3))
    legend_rows = math.ceil(len(models_order) / legend_ncol)
    fig.suptitle("Accuracy · Cost · Time, per model", fontsize=14, fontweight="bold",
                 x=0.02, y=0.998, ha="left", va="top")
    fig.text(0.02, 0.975, "Each panel independently sorted low → high",
              fontsize=9, color=MUTED_INK, ha="left", va="top")
    # Legend sits directly under the subtitle; how far down it extends
    # depends on its row count, which depends on how many models there are
    # — computed explicitly rather than a fixed offset so it stays clear of
    # the first panel's own title at any model count.
    legend_top = 0.945
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, legend_top),
               ncol=legend_ncol, fontsize=7.5, frameon=False)
    plot_top = legend_top - legend_rows * 0.028 - 0.02

    missing_time = [m for m in models_order if stats[m]["sec_per_img"] is None]
    if missing_time:
        fig.text(0.02, -0.01, f"Note: no timing data for {', '.join(missing_time)} "
                  f"— pass --times <out-prefix>_times.json from run_multi_model.py.",
                  fontsize=7.5, color=MUTED_INK)

    plt.tight_layout(rect=[0, 0, 1, plot_top])
    plot_path = f"{out_prefix}_metrics_dashboard.png"
    plt.savefig(plot_path, dpi=150, facecolor=PAGE_PLANE, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {plot_path}")


# ── PLOT 7: "Eval vs Cost/Time" Pareto-frontier scatter ─────────────────────
# Modeled directly on a reference chart (Ai2's OLMo-2 "Pareto frontier"
# plot): clean flat style (no hand-drawn/xkcd look), one labeled dot per
# model shaped+colored by provider family, and a shaded Pareto-frontier
# region showing which models are actually the efficient choice — not
# dominated by any other model that's both cheaper/faster AND at least as
# accurate.
PROVIDER_STYLE = {
    "gemini": {"marker": "*", "color": "#3b82f6", "label": "Gemini",        "size": 260},
    "gpt":    {"marker": "o", "color": "#ec4899", "label": "GPT",           "size": 150},
    "gemma":  {"marker": "D", "color": "#10b981", "label": "Ollama (local)", "size": 130},
}


def _provider_for_model(model: str) -> str:
    name = model.lower()
    if "gemini" in name:
        return "gemini"
    if "gemma" in name or "ollama" in name:
        return "gemma"
    return "gpt"


def _pareto_frontier(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """points is [(x, y), ...] where lower x and higher y are both better
    (cheaper/faster, more accurate). Returns the non-dominated subset,
    sorted by x ascending — a point is on the frontier if no other point
    has x <= this one's AND y >= this one's (strictly better in at least
    one, tied or better in the other)."""
    frontier = []
    for x, y in points:
        if not any((ox <= x and oy >= y and (ox, oy) != (x, y)) for ox, oy in points):
            frontier.append((x, y))
    return sorted(frontier)


def _smooth_step_curve(xs: list[float], ys: list[float], samples_per_segment: int = 60):
    """Turns a sequence of (x, y) waypoints into a dense, smoothly-eased
    curve instead of straight/right-angle segments — a cubic "smoothstep"
    ease (3t^2 - 2t^3) applied within each segment. ys here is already
    monotonically non-decreasing (a Pareto frontier's y only ever goes up
    as x increases), so easing can't introduce any overshoot/wiggle the
    way a general-purpose spline could — no scipy dependency needed for
    that guarantee."""
    curve_x, curve_y = [], []
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        y0, y1 = ys[i], ys[i + 1]
        for k in range(samples_per_segment):
            t = k / samples_per_segment
            t_eased = 3 * t * t - 2 * t * t * t
            curve_x.append(x0 + (x1 - x0) * t)
            curve_y.append(y0 + (y1 - y0) * t_eased)
    curve_x.append(xs[-1])
    curve_y.append(ys[-1])
    return curve_x, curve_y


def _plot_pareto_frontier(records: list[dict], out_prefix: str, times: dict[str, dict],
                            metric: str) -> None:
    """metric is "cost" (mean $ per 1000 specimens) or "time" (mean seconds
    per specimen, from --times — skipped entirely if that manifest wasn't
    given, same as the other time-dependent plots in this script)."""
    if not records:
        print(f"No records — skipping {metric} Pareto-frontier plot.")
        return

    models_order = []
    for r in records:
        if r["model"] not in models_order:
            models_order.append(r["model"])

    stats = {}
    for model in models_order:
        rows = [r for r in records if r["model"] == model]
        accuracy = sum(r["accuracy_pct"] for r in rows) / len(rows)
        if metric == "cost":
            value = (sum(r["cost_usd"] for r in rows) / len(rows)) * 1000
        else:
            value = times.get(model, {}).get("seconds_per_image")
        if value is None:
            continue
        stats[model] = (value, accuracy)

    if len(stats) < 2:
        print(f"Fewer than 2 models have {metric} data — skipping Pareto-frontier plot.")
        return

    model_list = list(stats.keys())
    xs = [stats[m][0] for m in model_list]
    ys = [stats[m][1] for m in model_list]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    fig, ax = plt.subplots(figsize=(13, 8))
    fig.patch.set_facecolor(PAGE_PLANE)
    ax.set_facecolor("white")

    x_pad = max((x_max - x_min) * 0.18, 1e-9)
    y_pad = max((y_max - y_min) * 0.25, 2.0)
    # NOT clamped to a min of 0 on the left — a free/local model (cost or
    # time == 0) has x_min == 0, and max(0, 0 - padding) collapses straight
    # back to 0, leaving that point sitting right on the y-axis line
    # instead of inside the plot with breathing room around it.
    xlim = (x_min - x_pad * 0.5, x_max + x_pad)
    ylim = (max(0, y_min - y_pad), y_max + y_pad)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)

    # Highlight band hugging just under the Pareto frontier — filling all
    # the way down to the bottom axis put mediocre, clearly-dominated
    # points (low score, still cheap) INSIDE the yellow zone and left the
    # actual best model sitting right on the boundary edge, which reads
    # backwards: yellow should mark "near the best," not "everything
    # achievable." A band of a fixed height under the curve keeps only the
    # models actually close to the frontier inside yellow; everything
    # further below sits on plain white.
    frontier = _pareto_frontier(list(zip(xs, ys)))
    fx = [xlim[0]] + [p[0] for p in frontier] + [xlim[1]]
    fy = [frontier[0][1]] + [p[1] for p in frontier] + [frontier[-1][1]]
    curve_x, curve_y = _smooth_step_curve(fx, fy)
    band_height = (ylim[1] - ylim[0]) * 0.14
    curve_y_bottom = [max(ylim[0], y - band_height) for y in curve_y]
    ax.fill_between(curve_x, curve_y_bottom, curve_y, color="#fff3c4", zorder=1)
    ax.plot(curve_x, curve_y, linestyle=(0, (6, 4)), color="#d68910", linewidth=2.0, zorder=2)

    ax.grid(True, linestyle="--", linewidth=0.7, color="#d8dce0", zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color("#37474f")
        ax.spines[side].set_linewidth(1.3)

    title = "Eval vs Cost" if metric == "cost" else "Eval vs Time"
    ax.text(0.0, 1.12, title, transform=ax.transAxes, fontsize=24, fontweight="bold",
            color="#1a2e35", ha="left", va="bottom")

    # Axes-fraction position of every point, to catch the rare case where
    # two models land close enough together that stacking both labels at
    # the same fixed (9, 7) offset would overlap — most points in this
    # data are spread out enough that a fixed offset is fine, but a tight
    # cluster (e.g. a free local model right next to a cheap API model)
    # isn't rare enough to ignore.
    placed_anchors = []
    seen_providers = []
    for i, model in enumerate(model_list):
        provider = _provider_for_model(model)
        if provider not in seen_providers:
            seen_providers.append(provider)
        style = PROVIDER_STYLE[provider]
        # A soft dark "shadow" marker drawn slightly larger and behind the
        # real one gives each point some visual weight/pop against the
        # yellow fill, instead of a flat marker sitting directly on it.
        ax.scatter([xs[i]], [ys[i]], marker=style["marker"], s=style["size"] * 1.35,
                   color="#1a2e35", alpha=0.18, zorder=3.5, linewidth=0)
        ax.scatter([xs[i]], [ys[i]], marker=style["marker"], s=style["size"],
                   color=style["color"], edgecolor="white", linewidth=1.6, zorder=4)

        ax_x = (xs[i] - xlim[0]) / (xlim[1] - xlim[0])
        ax_y = (ys[i] - ylim[0]) / (ylim[1] - ylim[0])
        crowded = any(abs(ax_x - px) < 0.08 and abs(ax_y - py) < 0.05 for px, py in placed_anchors)
        offset = (9, -16) if crowded else (9, 7)
        va = "top" if crowded else "bottom"
        ax.annotate(model, (xs[i], ys[i]), xytext=offset, textcoords="offset points",
                    fontsize=13, color=style["color"], fontweight="bold", zorder=5, va=va)
        placed_anchors.append((ax_x, ax_y))

    handles = [plt.Line2D([0], [0], marker=PROVIDER_STYLE[p]["marker"], linestyle="",
                           color=PROVIDER_STYLE[p]["color"], markersize=13,
                           label=PROVIDER_STYLE[p]["label"])
               for p in seen_providers]
    legend = ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0),
                        ncol=len(handles), frameon=True, fontsize=13, handletextpad=0.5,
                        columnspacing=1.6, borderpad=0.7)
    legend.get_frame().set_facecolor("white")
    legend.get_frame().set_edgecolor("#d8dce0")
    legend.get_frame().set_linewidth(1.0)

    ax.set_ylabel("Score (HerbAudit Accuracy %)", fontsize=15, color="#1a2e35", fontweight="bold")
    if metric == "cost":
        ax.set_xlabel("cost for 1000 specimens", fontsize=15, color="#1a2e35", fontweight="bold")
    else:
        ax.set_xlabel("Seconds per specimen", fontsize=15, color="#1a2e35", fontweight="bold")
    ax.tick_params(labelsize=12, colors="#37474f")

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    plot_path = f"{out_prefix}_eval_vs_{metric}.png"
    plt.savefig(plot_path, dpi=150, facecolor=PAGE_PLANE)
    plt.close(fig)
    print(f"Saved {plot_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+",
                     help="One or more herbaudit output directories (each containing "
                          "*_audit.json files) and/or individual *_audit.json file paths.")
    ap.add_argument("--gold-standard", default=None, metavar="XLSX_PATH",
                     help="Path to the Gold-Standard.xlsx specimen categorization (columns: "
                          "Institution, filename, Catalog Number, Type, GBIF, labelstudio, level). "
                          "Optional — only the by-category/resolution plots need it; the "
                          "cost/time/metrics-dashboard/sketch plots run without it.")
    ap.add_argument("--times", default=None, metavar="JSON_PATH",
                     help="Timing manifest written by run_multi_model.py "
                          "(<out-prefix>_times.json) — adds per-model timing to the "
                          "cost/accuracy plot. Optional: that plot still renders without "
                          "it, just without time annotations.")
    ap.add_argument("--out-prefix", default="model_comparison")
    args = ap.parse_args()

    records = _load_records(args.inputs)
    if not records:
        sys.exit("No usable *_audit.json records found.")

    models = sorted({r["model"] for r in records})
    print(f"Loaded {len(records)} record(s) across {len(models)} model(s): {', '.join(models)}")

    times = _load_times(args.times)
    _plot_cost_accuracy_time(records, args.out_prefix, times)
    _plot_pareto_frontier(records, args.out_prefix, times, metric="cost")
    _plot_pareto_frontier(records, args.out_prefix, times, metric="time")

    if not args.gold_standard:
        print("No --gold-standard given — skipping the by-category/resolution plots.")
        return

    matched = _match_gold_standard_rows(records, args.gold_standard)
    if not matched:
        sys.exit("No records matched the Gold Standard file — nothing else to plot.")

    _plot_accuracy_boxplot_by_category(matched, args.out_prefix)


if __name__ == "__main__":
    main()
