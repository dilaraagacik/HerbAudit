"""Download specimen images for a list of GBIF occurrence links (--gbif-links).

Each image is saved as <gbifID>.<ext>, because the audit matches an image to its
GBIF record by the file name's stem. Only the first still image of an occurrence
is downloaded, so the stem stays a clean GBIF id.
"""
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .images import IMAGE_EXTENSIONS

_API_URL = "https://api.gbif.org/v1/occurrence/"
_HEADERS = {"User-Agent": "HerbAudit (+https://github.com/dilaraagacik/HerbAudit)"}
_LINK_RE = re.compile(r"gbif\.org/(?:[\w-]+/)?(?:v1/)?occurrence/(\d+)", re.I)
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif",
    "image/webp": ".webp", "image/bmp": ".bmp",
}


def is_gbif_links(input_path: str) -> bool:
    """True if the input is a single GBIF occurrence link or a .txt list of links
    or ids. CSV/Excel files are always extraction results, never link lists."""
    if _LINK_RE.search(input_path):
        return True
    path = Path(input_path)
    return path.is_file() and path.suffix.lower() == ".txt"


def parse_gbif_ids(source: str) -> list[str]:
    """GBIF occurrence ids from a link, or a .txt file of them, in order and
    without duplicates. A line can hold a gbif.org/occurrence/<id> link anywhere
    in it, or be just a bare id."""
    ids: dict[str, None] = {}
    path = Path(source)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else [source]
    for line in lines:
        line = line.strip().strip('"')
        if line.isdigit():
            ids[line] = None
            continue
        for match in _LINK_RE.finditer(line):
            ids[match.group(1)] = None
    return list(ids)


def _get(session: requests.Session, url: str, retries: int = 3, **kwargs) -> requests.Response:
    """GET with a few retries on connection errors, timeouts and 429/5xx."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=60, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise last


def _existing_image(out_dir: Path, gbif_id: str) -> Path | None:
    return next((p for p in out_dir.glob(f"{gbif_id}.*") if p.suffix.lower() in IMAGE_EXTENSIONS), None)


def _download_one(session: requests.Session, gbif_id: str, out_dir: Path) -> tuple[str, str, str]:
    """Returns (gbif_id, status, detail); status is downloaded, cached or failed."""
    if _existing_image(out_dir, gbif_id):
        return gbif_id, "cached", ""
    try:
        resp = _get(session, _API_URL + gbif_id)
        if resp.status_code != 200:
            return gbif_id, "failed", f"GBIF API HTTP {resp.status_code}"
        media = [m for m in resp.json().get("media", [])
                 if m.get("type") == "StillImage" and m.get("identifier")]
        if not media:
            return gbif_id, "failed", "no image in GBIF record"

        image_url = media[0]["identifier"]
        img = _get(session, image_url, stream=True)
        content_type = img.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if img.status_code != 200:
            return gbif_id, "failed", f"image HTTP {img.status_code}: {image_url}"
        if not content_type.startswith("image/"):
            return gbif_id, "failed", f"not an image ({content_type or 'no content type'}): {image_url}"

        suffix = _CONTENT_TYPE_EXT.get(content_type) or Path(image_url.split("?")[0]).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        target = out_dir / f"{gbif_id}{suffix}"
        part = target.with_suffix(target.suffix + ".part")
        with open(part, "wb") as fh:
            for chunk in img.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        part.replace(target)
        return gbif_id, "downloaded", image_url
    except Exception as exc:   # one bad record must not stop the batch
        return gbif_id, "failed", f"{type(exc).__name__}: {exc}"


def download_gbif_images(links_file: str, out_dir: str, workers: int = 4) -> Path:
    """Download the images behind every GBIF link in links_file into out_dir.
    Existing images are skipped, so a rerun only fetches what is missing.
    Failures are listed in out_dir/download_log.csv. Returns out_dir."""
    ids = parse_gbif_ids(links_file)
    if not ids:
        raise ValueError(f"No GBIF occurrence links or ids found in {links_file}")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Downloading images for {len(ids)} GBIF records into {out} ...")

    with requests.Session() as session:
        session.headers.update(_HEADERS)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda i: _download_one(session, i, out), ids))

    counts = {s: sum(1 for r in results if r[1] == s) for s in ("downloaded", "cached", "failed")}
    failures = [r for r in results if r[1] == "failed"]
    log_path = out / "download_log.csv"
    if failures:
        with open(log_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["gbif_id", "reason"])
            writer.writerows((r[0], r[2]) for r in failures)
    elif log_path.exists():
        log_path.unlink()

    print(f"  {counts['downloaded']} downloaded, {counts['cached']} already present, "
          f"{counts['failed']} failed" + (f" (see {log_path})" if failures else ""))
    return out
