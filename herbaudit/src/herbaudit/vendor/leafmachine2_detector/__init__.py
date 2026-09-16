"""
HerbAudit's entry point into the vendored LeafMachine2 Archival Component
Detector. Everything in models/ and utils/ is vendored from LeafMachine2
(GPL-3.0) — see NOTICE.md for provenance. This file itself is original
HerbAudit code, not vendored.
"""
import logging
import sys
import warnings
from pathlib import Path

_VENDOR_ROOT = Path(__file__).resolve().parent
if str(_VENDOR_ROOT) not in sys.path:
    # Mirrors LeafMachine2's own detect.py bootstrap: the pickled checkpoint's
    # model object references `models.yolo.Model`, `models.common.Conv`, etc.
    # as top-level module paths, so models/utils must resolve as top-level
    # packages (not nested under herbaudit.vendor....) for torch.load to
    # unpickle it correctly.
    sys.path.insert(0, str(_VENDOR_ROOT))

import cv2
import numpy as np
import torch

with warnings.catch_warnings():
    # utils/general.py (vendored, GPL-3.0 — not ours to edit) does
    # `import pkg_resources`, which is deprecated in modern setuptools.
    # Silencing it here rather than upstream, scoped narrowly to just this
    # import so nothing else gets its warnings swallowed.
    warnings.filterwarnings("ignore", message=r".*pkg_resources is deprecated.*")
    from models.experimental import attempt_load  # noqa: E402
    from utils.augmentations import letterbox  # noqa: E402
    from utils.general import check_img_size, non_max_suppression, scale_coords  # noqa: E402
    from utils.torch_utils import select_device  # noqa: E402

# utils/general.py's own module-level `set_logging()` call (which runs as a
# side effect of the import above) attaches a StreamHandler straight to the
# ROOT logger — not its own "yolov5" logger — with no check for one already
# being there. If anything else in the process (google-genai, httpx, ...) has
# already added its own root handler by this point, every log line from here
# on (this module's own YOLOv5 banner/"Fusing layers"/"Model summary", httpx's
# request logging, herbaudit's own logging) gets emitted once per handler —
# i.e. duplicated. Collapse same-type/same-format duplicates down to one,
# right here, before _load_model()/run_archival_detector() ever log anything.
_root = logging.getLogger()
_seen: set[tuple] = set()
_keep = []
for _h in _root.handlers:
    _key = (type(_h), getattr(_h, "level", None), getattr(getattr(_h, "formatter", None), "_fmt", None))
    if _key in _seen:
        continue
    _seen.add(_key)
    _keep.append(_h)
_root.handlers = _keep
del _root, _seen, _keep

__all__ = ["run_archival_detector"]

_MODEL_CACHE: dict[str, torch.nn.Module] = {}


def _load_model(weights: str, device: str = ""):
    key = f"{weights}::{device}"
    if key not in _MODEL_CACHE:
        dev = select_device(device)
        _MODEL_CACHE[key] = attempt_load(weights, map_location=dev)
    return _MODEL_CACHE[key]


def run_archival_detector(
    image_path: str,
    weights: str,
    imgsz: int = 1280,
    conf_thres: float = 0.5,
    iou_thres: float = 0.45,
    device: str = "",
) -> list[tuple[str, float, tuple[int, int, int, int]]]:
    """
    Run LeafMachine2's Archival Component Detector on a single image.

    Returns [(class_name, confidence, (x1, y1, x2, y2)), ...] in original
    image pixel coordinates. class_name will be one of LeafMachine2's
    archival classes (e.g. 'label', 'barcode', 'ruler', 'colorcard', 'map',
    'envelope', 'photo') as embedded in the checkpoint's own class names.
    """
    model  = _load_model(weights, device)
    stride = int(model.stride.max())
    names  = model.names  # list[str] or {class_id: class_name}
    imgsz  = check_img_size(imgsz, s=stride)

    im0 = cv2.imread(str(image_path))
    if im0 is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")

    im = letterbox(im0, imgsz, stride=stride, auto=True)[0]
    im = im.transpose((2, 0, 1))[::-1]  # BGR->RGB, HWC->CHW
    im = np.ascontiguousarray(im)

    device_t = next(model.parameters()).device
    im_t = torch.from_numpy(im).to(device_t).float() / 255
    if im_t.ndim == 3:
        im_t = im_t[None]

    with torch.no_grad():
        pred = model(im_t)[0]
    pred = non_max_suppression(pred, conf_thres, iou_thres)

    detections: list[tuple[str, float, tuple[int, int, int, int]]] = []
    det = pred[0]
    if det is not None and len(det):
        det[:, :4] = scale_coords(im_t.shape[2:], det[:, :4], im0.shape).round()
        for *xyxy, conf, cls in det.tolist():
            cls_id   = int(cls)
            cls_name = names[cls_id] if isinstance(names, (list, tuple)) else names.get(cls_id, str(cls_id))
            x1, y1, x2, y2 = (int(v) for v in xyxy)
            detections.append((cls_name, float(conf), (x1, y1, x2, y2)))

    return detections
