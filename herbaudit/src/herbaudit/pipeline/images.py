"""Image resizing, label-region detection, and collage building (OpenCV
contour-based, vendored LeafMachine2 YOLOv5, and ultralytics backends)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("herbaudit")

# Triggered once from HerbAudit.__init__ so cv2/np import lazily.
def _lazy_imports():
    global cv2, np
    import cv2
    import numpy as np

cv2 = np = None


def _dedupe_root_log_handlers() -> None:
    """Collapse duplicate root-logger handlers left behind when the vendored
    LeafMachine2/YOLOv5 package double-imports its utils.general module (each
    import attaches its own StreamHandler, doubling every log line). Idempotent."""
    root = logging.getLogger()
    seen: set[tuple] = set()
    keep = []
    for h in root.handlers:
        key = (type(h), getattr(h, "level", None), getattr(getattr(h, "formatter", None), "_fmt", None))
        if key in seen:
            continue
        seen.add(key)
        keep.append(h)
    root.handlers = keep


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


# Archival classes to keep when building a collage; everything else detected
# (ruler, colorcard, map, photo, envelope) is filtered out.
DETECTOR_TARGET_CLASSES = {"label", "barcode"}


MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",  ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp",
}


def _resize_max_size(image: np.ndarray, max_size: int, interpolation=None) -> np.ndarray:
    """Resize so the longest side is max_size, preserving aspect ratio — never upscales."""
    interpolation = interpolation if interpolation is not None else cv2.INTER_AREA
    h, w = image.shape[:2]
    longest_side = max(h, w)
    if longest_side <= max_size:
        return image
    scale = max_size / longest_side
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    return cv2.resize(image, (new_w, new_h), interpolation=interpolation)


def _full_image_fallback(raw, src_w: int, src_h: int, image_path: Path,
                          max_resolution: tuple[int, int] | None):
    """Resize+encode the raw source image to fit max_resolution's pixel budget,
    for whenever no collage is being sent. Returns (image_bytes, mime_type, width, height)."""
    img_to_send = raw
    if max_resolution and raw is not None:
        img_to_send = _resize_max_size(raw, max(max_resolution))
    if img_to_send is not None:
        _, buf = cv2.imencode(".png", img_to_send)
        h, w = img_to_send.shape[:2]
        return buf.tobytes(), "image/png", w, h
    image_bytes = image_path.read_bytes()
    mime_type   = MIME_MAP.get(image_path.suffix.lower(), "image/jpeg")
    return image_bytes, mime_type, src_w, src_h


def _gather_images(input_path: str) -> list[Path]:
    """Sorted image files under input_path — every file in it if it's a
    directory, or just itself if it's a single image file."""
    inp = Path(input_path)
    return sorted(
        p for p in (inp.rglob("*") if inp.is_dir() else [inp])
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )


def _pack_crops_mosaic(
    crops: list[np.ndarray], row_width_budget: int, gap: int = 8,
    background: tuple[int, int, int] = (220, 220, 220),
) -> np.ndarray:
    """Bin-pack crops at their native size into a compact canvas using a
    guillotine packer (best-short-side-fit placement). Crops are placed
    tallest-first so a tall/thin crop (e.g. a cm-ruler strip) doesn't get
    stranded after shorter-but-bigger crops have carved up the free space.
    Canvas is trimmed to the used bounding box; leftover area is filled
    with `background`. Original sheet reading order is not preserved.
    """
    if not crops:
        raise ValueError("_pack_crops_mosaic: no crops to pack")

    bin_w   = max(row_width_budget, max(c.shape[1] for c in crops))
    ordered = sorted(crops, key=lambda c: c.shape[0], reverse=True)

    free_rects: list[tuple[int, int, int, int]] = [(0, 0, bin_w, 10 ** 9)]
    placements: list[tuple[int, int, np.ndarray]] = []
    max_y = 0

    for crop in ordered:
        ch, cw = crop.shape[:2]
        # Best Short Side Fit: pick the free rect leaving the smallest
        # "short side" of slack.
        best_idx, best_score = None, None
        for idx, (fx, fy, fw, fh) in enumerate(free_rects):
            if cw <= fw and ch <= fh:
                score = (min(fw - cw, fh - ch), max(fw - cw, fh - ch))
                if best_score is None or score < best_score:
                    best_score, best_idx = score, idx
        if best_idx is None:
            # Wider than every free rect — shouldn't happen given bin_w
            # above, but open a fresh full-width strip below everything
            # placed so far rather than dropping the crop.
            free_rects.append((0, max_y + gap, bin_w, 10 ** 9))
            best_idx = len(free_rects) - 1

        fx, fy, fw, fh = free_rects.pop(best_idx)
        placements.append((fx, fy, crop))
        max_y = max(max_y, fy + ch)

        leftover_w = fw - cw - gap
        leftover_h = fh - ch - gap
        if leftover_w <= leftover_h:
            right  = (fx + cw + gap, fy, leftover_w, ch)
            bottom = (fx, fy + ch + gap, fw, leftover_h)
        else:
            right  = (fx + cw + gap, fy, leftover_w, fh)
            bottom = (fx, fy + ch + gap, cw, leftover_h)
        if right[2] > 0 and right[3] > 0:
            free_rects.append(right)
        if bottom[2] > 0 and bottom[3] > 0:
            free_rects.append(bottom)

    canvas_w = max(x + crop.shape[1] for x, _, crop in placements)
    canvas_h = max_y
    canvas   = np.full((canvas_h, canvas_w, 3), background, dtype=np.uint8)
    for x, y, crop in placements:
        ch, cw = crop.shape[:2]
        canvas[y:y + ch, x:x + cw] = crop
    return canvas


def build_text_collage(
    image_path: Path,
    min_area: float = 0.003,
    max_area: float = 0.60,
    padding: int = 12,
    collage_width: int = 1400,
) -> np.ndarray | None:
    img = cv2.imread(str(image_path))
    if img is None:
        raise RuntimeError(f"Cannot open image: {image_path}")
    h, w       = img.shape[:2]
    total_area = h * w
    gray       = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thresh     = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    kernel   = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 8))
    closed   = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    crops: list[tuple[int, int, int, int, np.ndarray]] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        if not (min_area * total_area < area < max_area * total_area):
            continue
        asp = cw / ch if ch > 0 else 0
        if asp > 20 or asp < 0.1:
            continue
        x1, y1 = max(0, x - padding), max(0, y - padding)
        x2, y2 = min(w, x + cw + padding), min(h, y + ch + padding)
        crops.append((y1, x1, y2, x2, img[y1:y2, x1:x2]))

    if not crops:
        return None

    crops.sort(key=lambda c: (c[0] // 100, c[1]))
    return _pack_crops_mosaic([crop for _, _, _, _, crop in crops], collage_width)


def _merge_overlapping_boxes(
    boxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Iteratively merge boxes whose rectangles overlap into single encompassing
    boxes, collapsing duplicate YOLO detections of the same physical label.
    Call per class, not across all boxes — different labels that merely touch
    shouldn't merge. A merge requires intersection area of at least
    _MIN_OVERLAP_FRAC of the smaller box's area, checked at each step rather
    than on the final merged result, so a chain of small stray overlaps can't
    accumulate into an unwanted merge.
    """
    _MIN_OVERLAP_FRAC = 0.15
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        i = 0
        while i < len(boxes):
            j = i + 1
            while j < len(boxes):
                x1a, y1a, x2a, y2a = boxes[i]
                x1b, y1b, x2b, y2b = boxes[j]
                if x1a < x2b and x2a > x1b and y1a < y2b and y2a > y1b:
                    ix1, iy1 = max(x1a, x1b), max(y1a, y1b)
                    ix2, iy2 = min(x2a, x2b), min(y2a, y2b)
                    intersection = (ix2 - ix1) * (iy2 - iy1)
                    area_a = (x2a - x1a) * (y2a - y1a)
                    area_b = (x2b - x1b) * (y2b - y1b)
                    if intersection < _MIN_OVERLAP_FRAC * min(area_a, area_b):
                        j += 1
                        continue
                    boxes[i] = (min(x1a, x1b), min(y1a, y1b), max(x2a, x2b), max(y2a, y2b))
                    del boxes[j]
                    merged = True
                else:
                    j += 1
            i += 1
    return boxes


def build_label_collage_yolo(
    image_path: Path,
    weights_path: str,
    confidence: float = 0.5,
    imgsz: int = 1280,
    padding: int = 20,
    collage_width: int = 1400,
    target_classes: set | None = None,
) -> np.ndarray | None:
    """Use a YOLO model to detect label / archival regions on the herbarium sheet
    and stitch them into a collage — same output format as build_text_collage.

    Returns None on any error, or when nothing matched target_classes; the
    caller sends the untouched full image in that case rather than falling
    back to OpenCV, since OpenCV's contour detector can't filter by class.
    """
    try:
        from herbaudit.vendor.leafmachine2_detector import run_archival_detector
    except ImportError as exc:
        log.warning("LeafMachine2 detector unavailable (%s) — using OpenCV", exc)
        return None
    _dedupe_root_log_handlers()

    img = cv2.imread(str(image_path))
    if img is None:
        return None
    H, W = img.shape[:2]

    try:
        detections = run_archival_detector(
            image_path = str(image_path),
            weights    = weights_path,
            imgsz      = imgsz,
            conf_thres = confidence,
            iou_thres  = 0.45,
        )
    except Exception as exc:
        log.warning("LeafMachine2 detector inference failed: %s", exc)
        return None

    if not detections:
        return None

    # Group by class so only same-class overlapping detections get merged.
    boxes_by_class: dict[str, list[tuple[int, int, int, int]]] = {}
    for cls_name, conf, (x1, y1, x2, y2) in detections:
        cls_name = cls_name.lower()
        if target_classes and cls_name not in target_classes:
            continue
        boxes_by_class.setdefault(cls_name, []).append((x1, y1, x2, y2))

    crops: list[tuple[int, int, int, int, np.ndarray]] = []
    for cls_name, boxes in boxes_by_class.items():
        merged_boxes = _merge_overlapping_boxes(boxes)
        if len(merged_boxes) < len(boxes):
            log.info("  LeafMachine2 merged %d overlapping '%s' box(es) into %d",
                     len(boxes), cls_name, len(merged_boxes))
        for x1, y1, x2, y2 in merged_boxes:
            x1p, y1p = max(0, x1 - padding), max(0, y1 - padding)
            x2p, y2p = min(W, x2 + padding), min(H, y2 + padding)
            crop = img[y1p:y2p, x1p:x2p]
            if crop.size == 0:
                continue
            crops.append((y1p, x1p, y2p, x2p, crop))
            log.info("  LeafMachine2 detected '%s' @ [%d,%d,%d,%d]", cls_name, x1p, y1p, x2p, y2p)

    if not crops:
        log.warning("LeafMachine2 detector found no matching label regions — sending full image")
        return None

    # Sort top-to-bottom, left-to-right (same as OpenCV collage)
    crops.sort(key=lambda c: (c[0] // 100, c[1]))

    print(f"  LeafMachine2 detected {len(crops)} region(s)")
    return _pack_crops_mosaic([crop for _, _, _, _, crop in crops], collage_width)


_ULTRALYTICS_MODEL_CACHE: dict[str, Any] = {}
_ULTRALYTICS_DEVICE_CACHE: str | None = None


def _ultralytics_device() -> str:
    """Pick 'cuda:0' when a GPU is actually available, else 'cpu' — cached
    after the first call so the CUDA probe only runs once per process."""
    global _ULTRALYTICS_DEVICE_CACHE
    if _ULTRALYTICS_DEVICE_CACHE is None:
        try:
            import torch
            _ULTRALYTICS_DEVICE_CACHE = "cuda:0" if torch.cuda.is_available() else "cpu"
        except Exception:
            _ULTRALYTICS_DEVICE_CACHE = "cpu"
    return _ULTRALYTICS_DEVICE_CACHE


def build_label_collage_ultralytics(
    image_path: Path,
    weights_path: str,
    confidence: float = 0.5,
    padding: int = 20,
    collage_width: int = 1400,
    target_classes: set | None = None,
) -> np.ndarray | None:
    """Same job as build_label_collage_yolo(), but via the `ultralytics` package
    instead of the vendored LeafMachine2 YOLOv5 loader — use this for
    checkpoints the vendored loader can't read (YOLOv8/v11/v12, YOLO-World, etc.).

    `ultralytics` is AGPL-3.0 and not installed by default, so this stays
    opt-in via --detector ultralytics.

    Returns None on any error or when nothing matched target_classes, same as
    build_label_collage_yolo().
    """
    try:
        # .pt checkpoints load via torch.load() (pickle underneath), so a
        # malicious weights file can execute code on load. Default safe-load
        # on (setdefault lets an explicit override still work); this doesn't
        # cover the vendored LeafMachine2 loader, which hardcodes
        # weights_only=False. Only load weights files you trust.
        os.environ.setdefault("ULTRALYTICS_SAFE_LOAD", "1")
        from ultralytics import YOLO
    except ImportError as exc:
        log.warning("ultralytics not installed (%s) — pip install ultralytics, or use OpenCV/--detector yolo", exc)
        return None

    img = cv2.imread(str(image_path))
    if img is None:
        return None
    H, W = img.shape[:2]

    try:
        if weights_path not in _ULTRALYTICS_MODEL_CACHE:
            _ULTRALYTICS_MODEL_CACHE[weights_path] = YOLO(weights_path)
        model = _ULTRALYTICS_MODEL_CACHE[weights_path]
        # Auto-detect a GPU instead of hardcoding "cpu" for faster inference.
        device = _ultralytics_device()
        # Pass the already-decoded array instead of the path — otherwise
        # ultralytics re-opens and re-decodes the same (often large, raw-scan
        # resolution) file from disk a second time on every call.
        results = model(img, device=device, conf=confidence, verbose=False)
    except Exception as exc:
        log.warning("ultralytics inference failed: %s", exc)
        return None

    if not results or not results[0].boxes:
        return None

    names = results[0].names
    # Group by class so only same-class overlapping detections get merged.
    boxes_by_class: dict[str, list[tuple[int, int, int, int]]] = {}
    for box, cls_id in zip(results[0].boxes.xyxy.tolist(), results[0].boxes.cls.tolist()):
        cls_name = str(names[int(cls_id)]).lower()
        if target_classes and cls_name not in target_classes:
            continue
        x1, y1, x2, y2 = (int(v) for v in box)
        boxes_by_class.setdefault(cls_name, []).append((x1, y1, x2, y2))

    crops: list[tuple[int, int, int, int, np.ndarray]] = []
    for cls_name, boxes in boxes_by_class.items():
        merged_boxes = _merge_overlapping_boxes(boxes)
        if len(merged_boxes) < len(boxes):
            log.info("  ultralytics merged %d overlapping '%s' box(es) into %d",
                     len(boxes), cls_name, len(merged_boxes))
        for x1, y1, x2, y2 in merged_boxes:
            x1p, y1p = max(0, x1 - padding), max(0, y1 - padding)
            x2p, y2p = min(W, x2 + padding), min(H, y2 + padding)
            crop = img[y1p:y2p, x1p:x2p]
            if crop.size == 0:
                continue
            crops.append((y1p, x1p, y2p, x2p, crop))
            log.info("  ultralytics detected '%s' @ [%d,%d,%d,%d]", cls_name, x1p, y1p, x2p, y2p)

    if not crops:
        log.warning("ultralytics detector found no matching label regions — sending full image")
        return None

    crops.sort(key=lambda c: (c[0] // 100, c[1]))

    print(f"  ultralytics detected {len(crops)} region(s)")
    return _pack_crops_mosaic([crop for _, _, _, _, crop in crops], collage_width)
