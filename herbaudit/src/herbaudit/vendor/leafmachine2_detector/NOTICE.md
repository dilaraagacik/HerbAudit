# Vendored code notice

The `models/` and `utils/` subdirectories in this folder are vendored,
near-verbatim from:

- Project: [LeafMachine2](https://github.com/Gene-Weaver/LeafMachine2) —
  `leafmachine2/component_detector/{models,utils}/`
- This is LeafMachine2's own fork of Ultralytics YOLOv5, used there to run
  the Archival Component Detector (the model that locates labels, barcodes,
  rulers, etc. on a herbarium sheet).
- License: **GPL-3.0** (see `LICENSE` in this directory and the original
  per-file header `# YOLOv5 🚀 by Ultralytics, GPL-3.0 license`).
- Retrieved: 2026-07-08 from the `main` branch.

HerbAudit is itself licensed GPL-3.0-or-later (see repository root
`LICENSE`), so this vendoring is same-license and requires no relicensing.

## Why vendored instead of `pip install`-ed

LeafMachine2 is not published as an installable package, and pulling in
the full repository would drag in unrelated heavy dependencies (TensorFlow,
OpenVINO, GBIF download tooling, etc.) used by its other modules. Only the
detector's own inference dependencies are copied here.

## Modifications from upstream

Per GPL-3.0 §5, changes are marked at the point of change with a
`HerbAudit:` comment and dated. Summary:

- `models/common.py`: replaced one use of the deprecated `np.float` alias
  (removed in NumPy ≥1.24) with `float`. No behavioral change on the
  archival-detection path — that line is only reached by the optional
  `.pandas()` export method, which HerbAudit does not call.

- `utils/__init__.py`: upstream's file mixed two eras of content — an older
  GPL-3.0 section and, appended below it, a newer section explicitly headed
  `# YOLOv5 🚀 by Ultralytics, AGPL-3.0 license` containing a
  `notebook_init()` helper that imports the `ultralytics` pip package
  directly (`from ultralytics.utils.checks import check_requirements`).
  Nothing in this vendored tree calls `notebook_init()` or imports
  `emojis`/`TryExcept`/`threaded`/`join_threads` from `utils` (as opposed to
  from `utils.general`, which has its own independent copy of `emojis`), so
  the entire file was replaced with an empty package marker rather than
  carrying forward dead code with an inconsistent, stricter license than
  the rest of this tree.

- `utils/general.py`: replaced `pkg_resources.parse_version` (used by
  `check_version()`, called eagerly on herbaudit's actual inference path from
  `models/yolo.py`) with a small stdlib-only numeric-tuple comparator
  (`_parse_version_tuple()`). setuptools has been dropping `pkg_resources`
  from its own package in recent releases, so `import pkg_resources` at
  module scope crashed this file's import entirely on any environment with a
  newer setuptools — not a hypothetical, this broke real runs. The other
  `pkg_resources` usage (`check_requirements()`, for optional export-format
  dependency checks herbaudit's plain-`.pt` inference path never reaches) was
  left as-is functionally, just moved from a module-level import to a local
  one inside that function, so its absence no longer takes down the whole
  module — `check_requirements()` is already wrapped in `@try_except`
  upstream, so a missing `pkg_resources` there degrades to a logged error
  rather than a crash, same as any other unmet optional dependency.

No other files were modified. Everything else — `models/yolo.py`,
`models/experimental.py`, and the rest of `utils/` — is verbatim upstream
source.

## What is NOT vendored

LeafMachine2's `keypoint_detector/` module (leaf-shape/landmark
measurement, built on a newer, separately-licensed Ultralytics YOLOv8
fork) is intentionally excluded — HerbAudit only needs archival/label
detection, not morphological measurement, and this avoids any AGPL-3.0
entanglement.

## Entry point

`herbaudit/vendor/leafmachine2_detector/__init__.py` is original HerbAudit
code (not vendored) — it adds this directory to `sys.path` (mirroring how
LeafMachine2's own `detect.py` bootstraps itself) so the pickled model
checkpoint's internal module references (`models.yolo.Model`,
`models.common.Conv`, ...) resolve correctly on load, then exposes a single
`run_archival_detector()` function for HerbAudit's collage-building code.
