"""herbaudit.audit -- the audit/scoring/reporting half of the herbaudit package
(text, dates, geo, gbif, wfo, scoring, report, manual_ground_truth, run).
Re-exports every public name from its submodules."""

from .text import clean_text, strip_authors
from .dates import date_accuracy
from .geo import coord_accuracy
from .gbif import (
    safe_json,
    fetch_gbif_data,
    smart_get,
    check_gbif_backbone_fallback,
    check_label_synonym,
)
from .wfo import check_wfo_fallback, wfo_status_badge_html, check_synonym_fallback
from .scoring import EXCLUDED_SENTINEL, get_accuracy
from .report import (
    get_img,
    display_name,
    BADGE_COLORS,
    BADGE_TOOLTIPS,
    SYN_SRC_COLORS,
    SYN_SRC_DISPLAY,
    make_badge,
    score_bar,
    COL_MAP,
    build_card,
    build_no_reference_card,
)
from .manual_ground_truth import load_manual_annotations
from .run import (
    OUTPUT_HTML,
    OUTPUT_XLSX,
    DEFAULT_ANNOTATIONS_PATH,
    DEFAULT_TRANSCRIPTIONS_PATH,
    run_audit,
)

__all__ = [
    "clean_text",
    "strip_authors",
    "date_accuracy",
    "coord_accuracy",
    "safe_json",
    "fetch_gbif_data",
    "smart_get",
    "check_gbif_backbone_fallback",
    "check_label_synonym",
    "check_wfo_fallback",
    "wfo_status_badge_html",
    "check_synonym_fallback",
    "EXCLUDED_SENTINEL",
    "get_accuracy",
    "get_img",
    "display_name",
    "BADGE_COLORS",
    "BADGE_TOOLTIPS",
    "SYN_SRC_COLORS",
    "SYN_SRC_DISPLAY",
    "make_badge",
    "score_bar",
    "COL_MAP",
    "build_card",
    "build_no_reference_card",
    "load_manual_annotations",
    "OUTPUT_HTML",
    "OUTPUT_XLSX",
    "DEFAULT_ANNOTATIONS_PATH",
    "DEFAULT_TRANSCRIPTIONS_PATH",
    "run_audit",
]
