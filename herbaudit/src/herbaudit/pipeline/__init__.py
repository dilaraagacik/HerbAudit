"""herbaudit.pipeline -- the extraction-pipeline half of the herbaudit package
(schema, images, weights, cost, taxonomy, herb_audit). Re-exports every
public name from its submodules."""

from .schema import HERBARIUM_SCHEMA, SINGLE_PASS_PROMPT
from .images import (
    IMAGE_EXTENSIONS,
    DETECTOR_TARGET_CLASSES,
    MIME_MAP,
    build_text_collage,
    build_label_collage_yolo,
    build_label_collage_ultralytics,
)
from .weights import resolve_weights_backend, HERBAUDIT_CACHE_DIR, LEAFMACHINE2_RELEASE_URL
from .cost import MODEL_COSTS
from .taxonomy import taxonomic_audit
from .herb_audit import MEDIA_RESOLUTION_CHOICES, HerbAudit

__all__ = [
    "HERBARIUM_SCHEMA",
    "SINGLE_PASS_PROMPT",
    "IMAGE_EXTENSIONS",
    "DETECTOR_TARGET_CLASSES",
    "MIME_MAP",
    "build_text_collage",
    "build_label_collage_yolo",
    "build_label_collage_ultralytics",
    "resolve_weights_backend",
    "HERBAUDIT_CACHE_DIR",
    "LEAFMACHINE2_RELEASE_URL",
    "MODEL_COSTS",
    "taxonomic_audit",
    "MEDIA_RESOLUTION_CHOICES",
    "HerbAudit",
]
