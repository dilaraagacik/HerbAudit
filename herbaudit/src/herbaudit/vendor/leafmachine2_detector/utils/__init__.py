# YOLOv5, GPL-3.0 license
"""
utils/initialization

Modified 2026-07-08 by HerbAudit: upstream's utils/__init__.py contained a
`notebook_init()` Colab/Jupyter helper that imported the `ultralytics` pip
package directly (and was itself labeled AGPL-3.0 in a later section of the
same file, inconsistent with the GPL-3.0 header the rest of this vendored
tree carries). That function is unused by HerbAudit's detection path, so it
— along with the unused emojis/TryExcept/threaded/join_threads helpers
above it, none of which are imported elsewhere in this vendored tree — has
been removed rather than carried forward. See ../NOTICE.md.
"""
