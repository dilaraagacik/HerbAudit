"""
Detector-weights backend resolution (vendored LeafMachine2/YOLOv5 vs.
ultralytics) and auto-download of the default archival-detector checkpoint.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

import requests

from .images import _ULTRALYTICS_MODEL_CACHE

log = logging.getLogger("herbaudit")

def _dedupe_root_log_handlers() -> None:
    """Local copy of images._dedupe_root_log_handlers (replicated here to
    avoid a cross-module import)."""
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


_WEIGHTS_BACKEND_CACHE: dict[str, str | None] = {}


def resolve_weights_backend(weights_path: str) -> str | None:
    """
    Figure out (and cache) which loader can actually read *weights_path*: a
    LeafMachine2/YOLOv5 checkpoint or one trained with ultralytics' own
    tooling (YOLOv8/v11/v12, YOLO-World) — both are ".pt" files but need
    different loaders. Tries the vendored LeafMachine2 loader first; only
    attempts `ultralytics` if that fails and ultralytics is installed.
    Returns "leafmachine2", "ultralytics", or None if neither can load it.

    A successful LeafMachine2 load isn't enough on its own to confirm the
    backend: torch.load(weights_only=False) will happily unpickle an
    ultralytics checkpoint too when the `ultralytics` package is installed,
    returning the wrong object type. So the load is only trusted if the
    result is actually an instance of the vendored YOLOv5 Model class.
    """
    if weights_path in _WEIGHTS_BACKEND_CACHE:
        return _WEIGHTS_BACKEND_CACHE[weights_path]

    backend: str | None = None
    try:
        from herbaudit.vendor.leafmachine2_detector import _load_model as _lm2_load
        from models.yolo import Model as _LM2_Model  # vendored YOLOv5 model class
        model = _lm2_load(weights_path)
        if not isinstance(model, _LM2_Model):
            raise TypeError(
                f"loaded object is {type(model).__module__}.{type(model).__name__}, "
                f"not a LeafMachine2/YOLOv5 Model"
            )
        backend = "leafmachine2"
    except Exception as lm2_exc:
        try:
            os.environ.setdefault("ULTRALYTICS_SAFE_LOAD", "1")
            from ultralytics import YOLO
            _ULTRALYTICS_MODEL_CACHE[weights_path] = YOLO(weights_path)
            backend = "ultralytics"
        except Exception as ultra_exc:
            log.warning(
                "Could not load weights '%s' with either backend "
                "(leafmachine2: %s / ultralytics: %s)",
                weights_path, lm2_exc, ultra_exc,
            )
            backend = None

    _dedupe_root_log_handlers()  # the LeafMachine2 probe above may re-trigger it
    _WEIGHTS_BACKEND_CACHE[weights_path] = backend
    return backend


HERBAUDIT_CACHE_DIR = Path.home() / ".herbaudit" / "models"

# LeafMachine2's own official release — bundles every LeafMachine2 model into
# one zip; there's no per-model asset to fetch individually.
LEAFMACHINE2_RELEASE_URL = "https://github.com/Gene-Weaver/LeafMachine2/releases/download/v-2-1/release_v-2-1.zip"


def _find_archival_checkpoint(names: list[str]) -> str | None:
    """
    Pick the Archival Component Detector's checkpoint out of a LeafMachine2
    release zip's member list. Ships as `release_v-2-1/acd/best.pt` ("acd" =
    Archival Component Detector). Matches on an "acd" path segment primarily,
    with the old naming kept as a fallback for a future retitled release.
    """
    def has_segment(name: str, segment: str) -> bool:
        parts = re.split(r"[\\/]", name.lower())
        return segment in parts

    for pred in (
        lambda n: has_segment(n, "acd") and n.lower().endswith("best.pt"),
        lambda n: "archival_detector" in n.lower() and n.lower().endswith("best.pt"),
    ):
        matches = [n for n in names if pred(n)]
        if matches:
            return matches[0]
    return None


def _ensure_default_archival_weights(cache_path: Path | None = None) -> Path | None:
    """
    Auto-download LeafMachine2's Archival Component Detector checkpoint on
    first use. Downloads LEAFMACHINE2_RELEASE_URL (~1.4GB, one-time),
    extracts only the archival detector's checkpoint, caches it at
    *cache_path*, and discards the rest of the zip. A no-op once cached.

    The downloaded zip is cached separately until extraction succeeds, so a
    retry (e.g. after a _find_archival_checkpoint fix) can reuse it instead
    of re-fetching 1.4GB.

    Never raises — any failure logs a warning and returns None, so callers
    degrade to the OpenCV-contour fallback instead of crashing the run.
    """
    cache_path = cache_path or HERBAUDIT_CACHE_DIR / "archival_detector_best.pt"
    if cache_path.exists():
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path = cache_path.parent / "_leafmachine2_release.zip"

    try:
        if zip_path.exists():
            log.info("Reusing already-downloaded LeafMachine2 release at %s", zip_path)
        else:
            log.info("No archival detector checkpoint found at %s — downloading "
                      "LeafMachine2's release (~1.4GB, one-time only) ...", cache_path)
            tmp_path = zip_path.with_suffix(".zip.part")
            with requests.get(LEAFMACHINE2_RELEASE_URL, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                done = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            print(f"\r  Downloading LeafMachine2 release: "
                                  f"{done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
                if total:
                    print()
            tmp_path.rename(zip_path)  # only becomes the "cached" copy once fully written

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            chosen = _find_archival_checkpoint(names)
            if chosen is None:
                log.warning(
                    "LeafMachine2's release zip (%d entries) didn't contain an archival "
                    "detector checkpoint matching any known naming — falling back to "
                    "OpenCV. The zip is cached at %s if you want to inspect it yourself "
                    "(`unzip -l %s`) and pass the right file via --weights; a future fix "
                    "to _find_archival_checkpoint() in functions.py can reuse it without "
                    "re-downloading.", len(names), zip_path, zip_path,
                )
                return None
            with zf.open(chosen) as src, open(cache_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception as exc:
        log.warning(
            "Auto-download of LeafMachine2's archival detector weights failed (%s) — "
            "falling back to OpenCV. Download %s yourself and pass it via --weights if "
            "this persists.", exc, LEAFMACHINE2_RELEASE_URL,
        )
        if cache_path.exists():
            cache_path.unlink()  # partial/corrupt file from the failed attempt
        return None

    zip_path.unlink(missing_ok=True)  # extraction succeeded — reclaim ~1.4GB
    log.info("Cached archival detector checkpoint at %s", cache_path)
    return cache_path
