"""HerbAudit — the main extraction-pipeline class (Gemini / OpenAI / Ollama
providers), plus the small helpers only it uses."""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from .schema import HERBARIUM_SCHEMA, _normalize_record, _split_record, SINGLE_PASS_PROMPT
from .cost import (
    _calculate_cost, _openai_token_kwarg, _extract_text_from_batch_response,
    _BATCH_COST_DISCOUNT,
)
from .images import (
    _full_image_fallback, _gather_images, build_text_collage,
    build_label_collage_yolo, build_label_collage_ultralytics,
    DETECTOR_TARGET_CLASSES,
)
from .weights import resolve_weights_backend
from .taxonomy import taxonomic_audit
from . import images as _images_mod, cost as _cost_mod

log = logging.getLogger("herbaudit")

# genai/genai_types/openai/cv2/np are lazy-imported here; images.py and
# cost.py each keep their own separate cv2/np (and litellm) globals and are
# triggered separately from HerbAudit.__init__.
def _lazy_imports():
    global genai, genai_types, openai, cv2, np
    from google import genai
    from google.genai import types as genai_types
    import openai
    import cv2
    import numpy as np

genai = genai_types = openai = cv2 = np = None

_GEMINI_CONFIG = None  # initialised lazily in HerbAudit.__init__


def _prefetch_map(executor: ThreadPoolExecutor, fn, items, window: int):
    """Yield fn(item) for each item, in order — but compute up to `window` items
    ahead on *executor* so sequential work (e.g. rate-limited API calls) overlaps
    with the next item's prep instead of running strictly one at a time."""
    items = iter(items)
    futures = deque(executor.submit(fn, item) for item in itertools.islice(items, window))
    for item in items:
        yield futures.popleft().result()
        futures.append(executor.submit(fn, item))
    while futures:
        yield futures.popleft().result()


def _clean_taxon(name: str) -> str:
    name = re.sub(r"[\[\]()\"']", "", name)
    name = re.sub(r"\b(cf|aff|sp|ssp|var|subsp|f|subf|agg|s\.l|s\.s)\b\.?", "", name, flags=re.IGNORECASE)
    tokens = [t for t in name.strip().split() if t]
    return " ".join(tokens[:2])


MEDIA_RESOLUTION_CHOICES = ("default", "low", "medium", "high")


def _resolve_media_resolution(choice: str):
    """Map a CLI-friendly string to genai_types.MediaResolution. 'default' maps to
    None (omit the kwarg entirely) rather than passing UNSPECIFIED explicitly.
    Requires genai_types to already be lazy-imported."""
    if choice not in MEDIA_RESOLUTION_CHOICES:
        raise ValueError(
            f"Invalid media_resolution '{choice}' — choose one of {MEDIA_RESOLUTION_CHOICES}"
        )
    if choice == "default":
        return None
    attr_name = f"MEDIA_RESOLUTION_{choice.upper()}"
    try:
        return getattr(genai_types.MediaResolution, attr_name)
    except AttributeError:
        raise ValueError(
            f"media_resolution='{choice}' ({attr_name}) isn't supported by the installed "
            f"google-genai SDK — run 'pip install -U google-genai' to get it, or pick a "
            f"different value from {MEDIA_RESOLUTION_CHOICES}."
        ) from None


class HerbAudit:
    def __init__(self, gemini_api_key: str = None, gemini_model: str = "gemini-3.5-flash-lite",
                 openai_api_key: str = None, openai_model: str = "gpt-4o-mini",
                 ollama_model: str = None, ollama_host: str = "http://localhost:11434",
                 media_resolution: str = "high"):
        global _GEMINI_CONFIG
        _lazy_imports()
        _images_mod._lazy_imports()
        _cost_mod._lazy_imports()

        if not gemini_api_key and not openai_api_key and not ollama_model:
            raise ValueError("Provide gemini_api_key, openai_api_key, or ollama_model.")
        if media_resolution not in MEDIA_RESOLUTION_CHOICES:
            raise ValueError(
                f"Invalid media_resolution '{media_resolution}' — choose one of {MEDIA_RESOLUTION_CHOICES}"
            )
        self.media_resolution = media_resolution

        # Detector weights (optional — replaces OpenCV contour detection).
        # Accepts any YOLO-family .pt checkpoint; resolve_weights_backend()
        # picks the vendored LeafMachine2/YOLOv5 loader or the `ultralytics`
        # package as needed.
        self.weights = None   # set via run_full_audit or direct assignment

        # Gemini takes priority, then OpenAI, then local Ollama last.
        if gemini_api_key:
            self.provider    = "gemini"
            self.model       = gemini_model
            self.temperature = 0.0   # low + deterministic for extraction; incremented on retries
            self.client        = genai.Client(api_key=gemini_api_key)
            self.openai_client = None
            log.info("HerbAudit ready  [provider=gemini  model=%s  media_resolution=%s]",
                      gemini_model, media_resolution)
        elif openai_api_key:
            self.provider      = "openai"
            self.model         = openai_model
            self.openai_client = openai.OpenAI(api_key=openai_api_key)
            self.client        = None
            log.info("HerbAudit ready  [provider=openai  model=%s]", openai_model)
        else:
            # Ollama speaks the OpenAI chat-completions protocol, so the OpenAI
            # SDK client works unchanged against it with base_url pointed locally.
            self.provider      = "ollama"
            self.model         = ollama_model
            self.openai_client = openai.OpenAI(api_key="ollama", base_url=f"{ollama_host.rstrip('/')}/v1")
            self.client        = None
            log.info("HerbAudit ready  [provider=ollama  model=%s  host=%s]", ollama_model, ollama_host)

        self.results: list[dict[str, Any]] = []

    def _make_gemini_config(self, temperature: float):
        """Build a GenerateContentConfig with the given temperature."""
        _is_thinking_model = "2.5" in self.model
        media_res = _resolve_media_resolution(self.media_resolution)
        return genai_types.GenerateContentConfig(
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
            response_modalities=["TEXT"],
            temperature=temperature,
            **({"media_resolution": media_res} if media_res is not None else {}),
            **( {"thinking_config": genai_types.ThinkingConfig(thinking_budget=-1)}
                if _is_thinking_model else {} )
        )

    @staticmethod
    def _gemini_usage_tokens(usage) -> tuple[int, int]:
        """Pull (input, output) token counts from a Gemini SDK response's
        usage_metadata, folding thinking tokens into the output count —
        thoughts_token_count is billed as output but reported separately
        from candidates_token_count, unlike OpenAI where reasoning_tokens
        is already included in completion_tokens."""
        in_tok      = getattr(usage, "prompt_token_count",     0) or 0
        answer_tok  = getattr(usage, "candidates_token_count", 0) or 0
        thought_tok = getattr(usage, "thoughts_token_count",   0) or 0
        return in_tok, answer_tok + thought_tok

    def _call_gemini_single_pass(self, image_bytes: bytes, mime_type: str,
                                  retries: int = 3, delay: float = 3.0) -> tuple[dict, int, int]:
        """Single-pass Gemini: image → structured JSON in one API call.
        Temperature increments by +0.2 on each retry (like VoucherVision)."""
        image_part  = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        last_error: str = ""
        temperature = getattr(self, "temperature", 0.2)

        for attempt in range(1, retries + 1):
            cfg = self._make_gemini_config(temperature)
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=[SINGLE_PASS_PROMPT, image_part],
                    config=cfg,
                )
                raw = resp.text.strip()
                if raw.startswith("```"):
                    raw = "\n".join(
                        l for l in raw.split("\n") if not l.strip().startswith("```")
                    ).strip()
                parsed  = json.loads(raw)
                usage   = getattr(resp, "usage_metadata", None)
                in_tok, out_tok = self._gemini_usage_tokens(usage)
                thought_tok = getattr(usage, "thoughts_token_count", 0) or 0
                print(f"  Single-pass OK — in={in_tok} out={out_tok}"
                      f"{f' (incl. {thought_tok} thinking)' if thought_tok else ''} temp={temperature:.1f}")
                return parsed, in_tok, out_tok
            except json.JSONDecodeError as exc:
                last_error = f"JSON parse error: {exc} | raw: {repr(raw[:200])}"
                temperature = round(temperature + 0.2, 1)
                print(f"  Attempt {attempt}/{retries} – {last_error} → retry temp={temperature:.1f}")
                if attempt < retries:
                    try:
                        fix = self.client.models.generate_content(
                            model=self.model,
                            contents=f"Fix this to valid JSON only, no markdown:\n{raw}",
                            config=cfg,
                        )
                        usage   = getattr(fix, "usage_metadata", None)
                        in_tok, out_tok = self._gemini_usage_tokens(usage)
                        return json.loads(fix.text.strip()), in_tok, out_tok
                    except Exception:
                        pass
                time.sleep(delay * attempt)
            except Exception as exc:
                last_error = str(exc)
                temperature = round(temperature + 0.2, 1)
                print(f"  Attempt {attempt}/{retries} – Gemini API error: {exc} → retry temp={temperature:.1f}")
                if "400" in str(exc):
                    raise
                time.sleep(delay * attempt)
        print(f"  All {retries} Gemini attempts failed for this image.")
        print(f"    Last error: {last_error}")
        print(f"    Model: {self.model} | Image size: {len(image_bytes):,} bytes")
        log.error("All Gemini single-pass attempts failed: %s", last_error)
        raise RuntimeError(f"Gemini single-pass failed after {retries} attempts: {last_error}")

    def _call_openai(self, image_bytes: bytes, mime_type: str,
                     retries: int = 3, delay: float = 3.0) -> tuple[dict[str, Any], int, int]:
        import base64
        b64       = base64.b64encode(image_bytes).decode("utf-8")
        data_url  = f"data:{mime_type};base64,{b64}"
        last_error: str = ""
        token_budget = 2048   # bumped only if a response comes back empty (see below)
        for attempt in range(1, retries + 1):
            try:
                resp = self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": SINGLE_PASS_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                        ],
                    }],
                    **_openai_token_kwarg(self.model, token_budget),
                )
                raw = resp.choices[0].message.content.strip()
                diag = ""
                if not raw:
                    # Empty content on a reasoning model usually means reasoning
                    # tokens ate the whole budget before any answer text — double
                    # the budget for the next retry.
                    finish_reason = resp.choices[0].finish_reason
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    reasoning_tok = getattr(details, "reasoning_tokens", None) if details else None
                    diag = (f"  [finish_reason={finish_reason!r} reasoning_tokens={reasoning_tok!r} "
                            f"completion_tokens={getattr(resp.usage, 'completion_tokens', None)!r} "
                            f"budget={token_budget}]")
                    token_budget = min(token_budget * 2, 12000)
                print(f"  {self.provider.upper()} raw response ({len(raw)} chars): "
                      f"{repr(raw[:300])}{diag}")
                if raw.startswith("```"):
                    raw = "\n".join(
                        l for l in raw.split("\n") if not l.strip().startswith("```")
                    ).strip()
                parsed  = json.loads(raw)
                in_tok  = resp.usage.prompt_tokens     if resp.usage else 0
                out_tok = resp.usage.completion_tokens if resp.usage else 0
                print(f"  Parsed OK — tokens in={in_tok} out={out_tok}")
                return parsed, in_tok, out_tok

            except json.JSONDecodeError as exc:
                last_error = f"JSON parse error: {exc} | raw: {repr(raw[:200])}"
                print(f"  Attempt {attempt}/{retries} – {last_error}")
                time.sleep(delay * attempt)
            except Exception as exc:
                last_error = str(exc)
                print(f"  Attempt {attempt}/{retries} – OpenAI error: {exc}")
                if "400" in str(exc) or "invalid_image" in str(exc).lower():
                    raise
                time.sleep(delay * attempt)

        print(f"  All {retries} OpenAI attempts failed for this image.")
        print(f"    Last error: {last_error}")
        print(f"    Model: {self.model} | Image size: {len(image_bytes):,} bytes")
        log.error("All OpenAI attempts failed – returning empty record")
        return {k: "" for k in HERBARIUM_SCHEMA}, 0, 0

    def _cost_for(self, model: str, in_tok: int, out_tok: int) -> float:
        """Cost for a completed call. Local models (Ollama) are free and
        short-circuited here rather than falling back to _calculate_cost()'s
        generic paid-cloud rate."""
        if self.provider == "ollama":
            return 0.0
        return _calculate_cost(model, in_tok, out_tok, self.provider)

    def _build_collage_bytes(self, image_path: Path, max_resolution: tuple[int, int] | None = None,
                             use_collage: bool = True, output_dir: str | None = None) -> dict[str, Any]:
        """The CPU-bound "cropping" half of process_image — detect label regions,
        build the collage (or fall back to the full image), and encode it to bytes.
        Split out so run_full_audit can run this across a thread pool ahead of the
        sequential (rate-limited) LLM calls; cv2/YOLO release the GIL, so this
        genuinely parallelizes across CPU cores. Returns everything process_image needs.
        """
        collage_used = False

        # Read actual image dimensions first so we can cap the collage width
        raw = cv2.imread(str(image_path))
        src_h, src_w = (raw.shape[:2] if raw is not None else (0, 0))

        # Row-width budget for the mosaic packer — how many native-size crops
        # share a row before wrapping. Detection itself still runs against the
        # full-res source, so small/faint text stays detectable.
        collage_width = min(max(src_w, 1), max_resolution[0]) if max_resolution else max(src_w, 1)

        _det = getattr(self, "_detector", "auto")
        _detector_used = None   # "leafmachine2" | "ultralytics" | "opencv"
        try:
            if not use_collage:
                # --no-collage: skip detection, send full image
                collage = None
                log.info("Collage disabled — using full image")
            elif self.weights and _det != "opencv":
                # Backend auto-resolved from the weights file. No fallback to
                # OpenCV when the resolved backend finds nothing in
                # target_classes: OpenCV can't filter by class, so cascading to
                # it would defeat the filter. Sending the untouched full image
                # is the closer match to "only labels, nothing else".
                backend = resolve_weights_backend(self.weights)
                if backend == "leafmachine2":
                    collage = build_label_collage_yolo(
                        image_path,
                        weights_path  = self.weights,
                        collage_width = collage_width,
                        target_classes = DETECTOR_TARGET_CLASSES,
                    )
                    _detector_used = "leafmachine2"
                elif backend == "ultralytics":
                    # No filter here — an arbitrary ultralytics model can use
                    # any class names it wants, so LeafMachine2's vocabulary
                    # would silently match nothing.
                    collage = build_label_collage_ultralytics(
                        image_path,
                        weights_path  = self.weights,
                        collage_width = collage_width,
                        target_classes = None,
                    )
                    _detector_used = "ultralytics"
                else:
                    collage = None
            else:
                collage = build_text_collage(
                    image_path, collage_width=collage_width)
                _detector_used = "opencv"
        except Exception as exc:
            log.warning("Label detection failed (%s) — using full image", exc)
            collage = None

        img_w, img_h = src_w, src_h
        if collage is not None:
            img_h, img_w = collage.shape[:2]
            # If collage is too thin it means detection found nothing useful — discard
            if img_h < 200:
                log.warning("Collage too thin (%dpx) — falling back to full image", img_h)
                collage = None
                img_w, img_h = src_w, src_h

        if collage is not None:
            # No resize cap here — crops stay at native size; the only
            # remaining guard is the hard byte-size cap just below.
            if output_dir:
                collage_dir = Path(output_dir) / f"collaged_{_detector_used or 'unknown'}"
                collage_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(collage_dir / f"{image_path.stem}.png"), collage)
            _, buf      = cv2.imencode(".png", collage)
            image_bytes = buf.tobytes()
            # Hard size cap: if still > 10 MB, keep halving resolution until it fits
            _MAX_BYTES  = 10 * 1024 * 1024
            while len(image_bytes) > _MAX_BYTES and img_w > 200:
                img_w = max(200, img_w // 2)
                img_h = max(1,   img_h // 2)
                collage     = cv2.resize(collage, (img_w, img_h), interpolation=cv2.INTER_AREA)
                _, buf      = cv2.imencode(".png", collage)
                image_bytes = buf.tobytes()
                log.warning("Collage > 10 MB — resized to %dx%d (%d KB)", img_w, img_h, len(image_bytes)//1024)
            mime_type    = "image/png"
            collage_used = True
        else:
            image_bytes, mime_type, img_w, img_h = _full_image_fallback(
                raw, src_w, src_h, image_path, max_resolution)

        return {
            "image_bytes": image_bytes, "mime_type": mime_type,
            "img_w": img_w, "img_h": img_h, "collage_used": collage_used,
            "raw": raw, "src_w": src_w, "src_h": src_h,
            "detector_used": _detector_used if collage_used else None,
        }

    def process_image(self, image_path: Path, max_resolution: tuple[int, int] | None = None,
                      use_collage: bool = True, output_dir: str | None = None,
                      _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """*_payload*, when given (typically precomputed by run_full_audit's
        crop-ahead pool), skips redoing the detection/collage step."""
        payload = _payload if _payload is not None else self._build_collage_bytes(
            image_path, max_resolution, use_collage, output_dir)
        image_bytes  = payload["image_bytes"]
        mime_type    = payload["mime_type"]
        img_w        = payload["img_w"]
        img_h        = payload["img_h"]
        collage_used = payload["collage_used"]
        raw          = payload["raw"]
        src_w        = payload["src_w"]
        src_h        = payload["src_h"]
        detector_used = payload.get("detector_used")

        display_name = re.sub(r"^_resized_", "", image_path.name)

        # Single vision call for every provider — image straight to structured JSON.
        pipeline = self.provider if self.provider in ("openai", "ollama") else "gemini"

        pipeline_label = {
            "gemini": "GEMINI 1-PASS", "openai": "OPENAI 1-PASS", "ollama": "OLLAMA 1-PASS",
        }[pipeline]
        print(f"  {pipeline_label} ← {display_name}  [collage={collage_used}  {img_w}×{img_h}px  {len(image_bytes)//1024}KB]")

        try:
            if pipeline == "gemini":
                record, in_tok, out_tok = self._call_gemini_single_pass(image_bytes, mime_type)
            else:
                record, in_tok, out_tok = self._call_openai(image_bytes, mime_type)
        except Exception as exc:
            if collage_used:
                print(f"  ↩ Collage rejected ({exc}), retrying with full image")
                image_bytes, mime_type, img_w, img_h = _full_image_fallback(
                    raw, src_w, src_h, image_path, max_resolution)
                collage_used  = False
                detector_used = None   # the collage attempt was abandoned — full image sent instead
                # Retry with full image using same pipeline
                if pipeline == "gemini":
                    record, in_tok, out_tok = self._call_gemini_single_pass(image_bytes, mime_type)
                else:
                    record, in_tok, out_tok = self._call_openai(image_bytes, mime_type)
            else:
                raise
        record = _normalize_record(record)
        cost_usd = self._cost_for(self.model, in_tok, out_tok)
        _filled = sum(1 for k in HERBARIUM_SCHEMA if str(record.get(k, "")).strip())
        record["source_image"]  = image_path.name
        record["model"]         = self.model
        record["collage_used"]  = collage_used
        record["detector_used"] = detector_used
        record["tokens_in"]     = in_tok
        record["tokens_out"]    = out_tok
        record["cost_usd"]      = cost_usd
        record["image_width"]   = img_w
        record["image_height"]  = img_h
        record["precrop_width"]  = src_w
        record["precrop_height"] = src_h
        record["sent_image_kb"]   = round(len(image_bytes) / 1024, 1)
        record["source_image_kb"] = round(image_path.stat().st_size / 1024, 1)
        if self.provider == "gemini":
            record["media_resolution"] = self.media_resolution
        record["fields_extracted"]        = _filled
        record["fields_total"]            = len(HERBARIUM_SCHEMA)
        record["field_completeness_pct"]  = round(100 * _filled / len(HERBARIUM_SCHEMA), 1)
        log.info("  tokens in=%d out=%d  cost=$%.5f", in_tok, out_tok, cost_usd)
        return record

    def _build_batch_request_dict(self, image_bytes: bytes, mime_type: str, cfg) -> dict:
        """Build one batch-line "request" object via GenerateContentConfig's
        .to_json_dict(), minus "automatic_function_calling" — that field is
        client-side only and the batch endpoint's stricter parser 400s on it
        (unlike the live endpoint, which silently ignores it)."""
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        content = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=SINGLE_PASS_PROMPT), image_part],
        )
        cfg_dict = cfg.to_json_dict()
        cfg_dict.pop("automatic_function_calling", None)
        return {
            "contents": [content.to_json_dict()],
            "generation_config": cfg_dict,
        }

    def _load_cached_audit(self, json_path: Path) -> dict | None:
        """Return the cached *_audit.json record at json_path if it exists AND was
        produced by the model this run is currently using — else None, since the
        filename alone doesn't say which model produced a cached file."""
        if not json_path.exists():
            return None
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        cached_model = data.get("herbaudit_meta", data).get("model")
        if cached_model != self.model:
            return None
        return data

    def _run_full_audit_batch(
        self,
        images: list[Path],
        out: Path,
        skip_existing: bool,
        skip_taxonomy: bool,
        max_resolution: tuple[int, int] | None,
        use_collage: bool,
        output_dir: str,
        batch_chunk_mb: int,
        batch_submit_workers: int,
        batch_poll_interval: float,
    ) -> None:
        """Batch-API replacement for the live per-image loop below — same crop/
        collage prep, same *_audit.json cache, same self.results shape, but
        submitted as async Gemini Batch jobs (50% of live-call cost). Only
        meaningful for provider == "gemini"; caller enforces that.

        Chunking is done by encoded byte budget (~batch_chunk_mb per JSONL
        file) rather than a fixed image count, since the Batch API caps a
        single input file at 2GB and photo size varies.
        """
        out.mkdir(parents=True, exist_ok=True)

        cached: dict[Path, dict] = {}
        if skip_existing:
            for p in images:
                data = self._load_cached_audit(out / (p.stem + "_audit.json"))
                if data is not None:
                    cached[p] = data

        to_process = [p for p in images if p not in cached]
        for p in images:
            if p in cached:
                self.results.append(cached[p])

        if not to_process:
            print("Nothing to do — every image is already cached.")
            return

        if batch_chunk_mb > 1900:
            print(f"  --batch-chunk-mb={batch_chunk_mb} is close to the Batch API's "
                  f"2048 MB per-file cap — clamping to 1900 MB")
            batch_chunk_mb = 1900

        print(f"  Batch mode: building payloads for {len(to_process)} image(s)...")
        crop_workers = min(8, os.cpu_count() or 4, len(to_process))
        with ThreadPoolExecutor(max_workers=crop_workers) as pool:
            built = list(pool.map(
                lambda p: (p, self._build_collage_bytes(p, max_resolution, use_collage, output_dir)),
                to_process,
            ))
        payload_by_path = {p: pl for p, pl in built}

        chunk_budget = batch_chunk_mb * 1024 * 1024
        chunks: list[list[Path]] = []
        current: list[Path] = []
        current_bytes = 0
        for img_path in to_process:
            # base64 inflates raw bytes ~4/3; + a small constant for the
            # surrounding JSON/prompt text so the estimate errs conservative.
            encoded_size = int(len(payload_by_path[img_path]["image_bytes"]) * 4 / 3) + 4096
            if current and current_bytes + encoded_size > chunk_budget:
                chunks.append(current)
                current, current_bytes = [], 0
            current.append(img_path)
            current_bytes += encoded_size
        if current:
            chunks.append(current)

        print(f" {len(chunks)} batch chunk(s) for {len(to_process)} image(s) "
              f"(~{batch_chunk_mb} MB budget each)")

        def _submit_chunk(idx: int, chunk: list[Path]) -> dict:
            # Failures here are caught and reported as a failed chunk rather
            # than raised — one bad chunk shouldn't abort the rest.
            keys = {p.stem: p for p in chunk}
            jsonl_path = out / f"_batch_chunk_{idx}.jsonl"
            try:
                temperature = getattr(self, "temperature", 0.2)
                cfg = self._make_gemini_config(temperature)
                with open(jsonl_path, "w", encoding="utf-8") as f:
                    for img_path in chunk:
                        payload = payload_by_path[img_path]
                        request_dict = self._build_batch_request_dict(
                            payload["image_bytes"], payload["mime_type"], cfg)
                        line = {"key": img_path.stem, "request": request_dict}
                        f.write(json.dumps(line, ensure_ascii=False) + "\n")

                uploaded = self.client.files.upload(
                    file=str(jsonl_path),
                    config=genai_types.UploadFileConfig(
                        display_name=f"herbaudit-batch-{idx}", mime_type="jsonl"),
                )
                job = self.client.batches.create(
                    model=self.model,
                    src=uploaded.name,
                    config={"display_name": f"herbaudit-batch-{idx}"},
                )
                print(f"Chunk {idx + 1}/{len(chunks)} submitted — "
                      f"{len(chunk)} image(s) — job {job.name}")
                return {"job_name": job.name, "chunk_idx": idx, "keys": keys, "submit_error": None}
            except Exception as exc:
                print(f"Chunk {idx + 1}/{len(chunks)} failed to submit — {exc}")
                return {"job_name": None, "chunk_idx": idx, "keys": keys, "submit_error": str(exc)}
            finally:
                jsonl_path.unlink(missing_ok=True)

        with ThreadPoolExecutor(max_workers=min(batch_submit_workers, len(chunks))) as pool:
            job_infos = list(pool.map(lambda item: _submit_chunk(*item), enumerate(chunks)))

        _ENDED_STATES = {
            "JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
            "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED",
        }
        print(f" {len(chunks)} chunk(s) submitted — "
              f"polling every {batch_poll_interval:.0f}s until done "
              f"(target turnaround is minutes-to-24h)...")

        chunk_outcomes: dict[str, tuple] = {}   # image stem -> ("ok", record, in_tok, out_tok) | ("error", msg)
        n_done_chunks  = 0

        # Chunks that never got submitted (upload/create failed) are already-
        # ended — mark them now instead of polling a job name that doesn't exist.
        remaining = {}
        for info in job_infos:
            if info["submit_error"] is not None:
                for key in info["keys"]:
                    chunk_outcomes[key] = ("error", f"batch submission failed: {info['submit_error']}")
                n_done_chunks += 1
            else:
                remaining[info["chunk_idx"]] = info

        while remaining:
            for idx in list(remaining):
                info  = remaining[idx]
                job   = self.client.batches.get(name=info["job_name"])
                state = job.state.name if job.state else "JOB_STATE_UNSPECIFIED"
                if state not in _ENDED_STATES:
                    continue

                if state != "JOB_STATE_SUCCEEDED":
                    err = getattr(job, "error", None)
                    for key in info["keys"]:
                        chunk_outcomes[key] = ("error", f"batch job {state}: {err}")
                elif not (job.dest and job.dest.file_name):
                    for key in info["keys"]:
                        chunk_outcomes[key] = ("error", "batch job succeeded with no result file")
                else:
                    raw  = self.client.files.download(file=job.dest.file_name)
                    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    seen = set()
                    for line_no, line in enumerate(text.splitlines()):
                        if not line.strip():
                            continue
                        parsed = json.loads(line)
                        # Prefer the "key" the request line carried; fall back to
                        # positional order in case the SDK omits it on output.
                        key = parsed.get("key")
                        if key is None:
                            ordered_keys = list(info["keys"])
                            key = ordered_keys[line_no] if line_no < len(ordered_keys) else None
                        if key is None:
                            continue
                        seen.add(key)
                        if parsed.get("response"):
                            resp = parsed["response"]
                            try:
                                raw_text = _extract_text_from_batch_response(resp)
                                clean = raw_text.strip()
                                if clean.startswith("```"):
                                    clean = "\n".join(
                                        l for l in clean.split("\n") if not l.strip().startswith("```")
                                    ).strip()
                                record = json.loads(clean)
                                # thoughts_token_count folded into out_tok — see
                                # _gemini_usage_tokens's docstring.
                                usage = resp.get("usageMetadata") or resp.get("usage_metadata") or {}
                                in_tok  = usage.get("promptTokenCount",     usage.get("prompt_token_count", 0)) or 0
                                answer_tok = usage.get("candidatesTokenCount", usage.get("candidates_token_count", 0)) or 0
                                thought_tok = usage.get("thoughtsTokenCount", usage.get("thoughts_token_count", 0)) or 0
                                out_tok = answer_tok + thought_tok
                                chunk_outcomes[key] = ("ok", record, in_tok, out_tok)
                            except Exception as exc:
                                chunk_outcomes[key] = ("error", f"could not parse batch response: {exc}")
                        else:
                            chunk_outcomes[key] = ("error", str(parsed.get("error", "unknown batch error")))
                    for key in info["keys"]:
                        if key not in seen:
                            chunk_outcomes[key] = ("error", "no result returned for this key")

                n_done_chunks += 1
                print(f" Chunk {idx + 1}/{len(chunks)} finished ({state}) — "
                      f"{n_done_chunks}/{len(chunks)} total")
                del remaining[idx]
            if remaining:
                time.sleep(batch_poll_interval)

        # Fold every outcome through the same taxonomy-audit + cache path the
        # live loop uses below, so results.html generation is identical either way.
        n_ok, errors = 0, []
        for info in job_infos:
            for key, img_path in info["keys"].items():
                outcome   = chunk_outcomes.get(key, ("error", "no result returned for this key"))
                json_path = out / (img_path.stem + "_audit.json")

                if outcome[0] == "error":
                    err_msg = f"{img_path.name}: {outcome[1]}"
                    errors.append(err_msg)
                    print(f" ERROR — {err_msg}")
                    self.results.append({"source_image": img_path.name, "error": outcome[1]})
                    continue

                _, record, in_tok, out_tok = outcome
                record  = _normalize_record(record)
                payload = payload_by_path[img_path]
                cost_usd = self._cost_for(self.model, in_tok, out_tok)
                cost_usd = round(cost_usd * _BATCH_COST_DISCOUNT, 6)
                _filled = sum(1 for k in HERBARIUM_SCHEMA if str(record.get(k, "")).strip())
                record["source_image"]  = img_path.name
                record["model"]         = self.model
                record["collage_used"]  = payload["collage_used"]
                record["detector_used"] = payload.get("detector_used")
                record["tokens_in"]     = in_tok
                record["tokens_out"]    = out_tok
                record["cost_usd"]      = cost_usd
                record["image_width"]   = payload["img_w"]
                record["image_height"]  = payload["img_h"]
                record["precrop_width"]   = payload["src_w"]
                record["precrop_height"]  = payload["src_h"]
                record["sent_image_kb"]   = round(len(payload["image_bytes"]) / 1024, 1)
                record["source_image_kb"] = round(img_path.stat().st_size / 1024, 1)
                if self.provider == "gemini":
                    record["media_resolution"] = self.media_resolution
                record["fields_extracted"]       = _filled
                record["fields_total"]           = len(HERBARIUM_SCHEMA)
                record["field_completeness_pct"] = round(100 * _filled / len(HERBARIUM_SCHEMA), 1)

                taxon_raw   = record.get("scientificName", "").strip()
                taxon_clean = _clean_taxon(taxon_raw) if taxon_raw else ""
                record["taxon_clean"] = taxon_clean
                if skip_taxonomy:
                    record["tax_audit"] = {"status": "SKIPPED", "accepted": "", "sources": [], "conf": 0}
                else:
                    record["tax_audit"] = taxonomic_audit(taxon_clean) if taxon_clean else {
                        "status": "NOT_FOUND", "accepted": "", "sources": [], "conf": 0
                    }
                record = _split_record(record)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
                self.results.append(record)
                n_ok += 1

        print(f"\n Batch done — {n_ok}/{len(to_process)} extracted successfully")
        if errors:
            print(f" {len(errors)} error(s):")
            for e in errors:
                print(f"      • {e}")
            print(f"\n  Re-run with --batch (cached results are kept, skip_existing).")
        log.info("Batch audit done. %d ok, %d errors.", n_ok, len(errors))

    def run_full_audit(
        self,
        input_path: str,
        output_dir: str = "./herbaudit_output",
        delay: float = 0.5,
        skip_existing: bool = False,
        max_resolution: tuple[int, int] | None = (1536, 1536),
        skip_taxonomy: bool = False,
        weights: str | None = None,
        use_collage: bool = True,
        detector: str = "auto",
        batch: bool = False,
        batch_chunk_mb: int = 1000,
        batch_submit_workers: int = 5,
        batch_poll_interval: float = 30.0,
    ) -> None:
        if weights:
            self.weights = weights
        if not use_collage:
            print("  Collage disabled — full images sent to Gemini")
        self._use_collage = use_collage
        self._detector    = detector  # "auto" | "yolo" | "opencv"

        # Print detector mode once at start
        if use_collage:
            if self.weights and detector != "opencv":
                backend = resolve_weights_backend(self.weights)
                if backend == "leafmachine2":
                    print(f"Detector: LeafMachine2 Archival Component Detector ({self.weights})")
                elif backend == "ultralytics":
                    print(f"Detector: ultralytics ({self.weights})")
                else:
                    print(f"Could not load weights '{self.weights}' with any backend — using OpenCV")
            else:
                print("  Detector: OpenCV contours (pass --weights <path> for a trained detector)")
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        # Print the resolved path — output_dir defaults to a cwd-relative path,
        # so running from a different directory can silently miss the existing cache.
        print(f"  Output/cache dir: {out.resolve()}")
        images = _gather_images(input_path)
        log.info("Found %d image(s) to audit", len(images))

        if batch:
            if self.provider != "gemini":
                raise ValueError("Batch mode is only supported for the Gemini provider (Batch API).")
            return self._run_full_audit_batch(
                images=images, out=out, skip_existing=skip_existing, skip_taxonomy=skip_taxonomy,
                max_resolution=max_resolution, use_collage=use_collage, output_dir=output_dir,
                batch_chunk_mb=batch_chunk_mb,
                batch_submit_workers=batch_submit_workers, batch_poll_interval=batch_poll_interval,
            )

        try:
            from tqdm import tqdm as _tqdm
        except ImportError:
            _tqdm = None

        errors:  list[str] = []
        n_total = len(images)
        # A cached *_audit.json only counts if it was produced by the model
        # this run is using (see _load_cached_audit).
        cached: dict[Path, dict] = {}
        if skip_existing:
            for p in images:
                data = self._load_cached_audit(out / (p.stem + "_audit.json"))
                if data is not None:
                    cached[p] = data
        n_cached = len(cached)

        bar = _tqdm(
            images,
            total   = n_total,
            desc    = "Extracting",
            unit    = "img",
            ncols   = 80,
            colour  = "green",
            dynamic_ncols = True,
        ) if _tqdm else images

        # Crop-ahead pool: detection/collage-building (cv2 + YOLO) is CPU-bound
        # and releases the GIL, so this pool computes each image's collage a few
        # images ahead of the sequential (rate-limited) LLM calls that follow.
        _to_process = [p for p in images if p not in cached]
        _crop_workers = min(8, os.cpu_count() or 4, max(1, len(_to_process)))

        def _prep_one(img_path: Path):
            # Runs against the native-resolution source — _build_collage_bytes
            # already crops+resizes to fit max_resolution, so pre-shrinking here
            # would only throw away detail before the detector ever saw it.
            payload = self._build_collage_bytes(
                img_path, max_resolution=max_resolution,
                use_collage=getattr(self, "_use_collage", True), output_dir=output_dir,
            )
            return img_path, max_resolution, payload

        crop_pool = ThreadPoolExecutor(max_workers=_crop_workers)
        prepped   = _prefetch_map(crop_pool, _prep_one, _to_process, _crop_workers)

        for img_path in bar:
            if img_path in cached:
                if _tqdm:
                    bar.set_postfix_str(f"cached: {img_path.name}", refresh=True)
                else:
                    print(f"  ↩ Cached: {img_path.name}")
                log.info("Skip (exists): %s", img_path.name)
                self.results.append(cached[img_path])
                continue

            json_path = out / (img_path.stem + "_audit.json")

            if _tqdm:
                bar.set_postfix_str(img_path.name[:40], refresh=True)
            else:
                n_done = len(self.results)
                print(f"  [{n_done+1}/{n_total}] {img_path.name}")

            log.info("── %s", img_path.name)
            _t_img = time.perf_counter()

            actual_path, _effective_res, _payload = next(prepped)

            try:
                record = self.process_image(actual_path, max_resolution=_effective_res,
                                            use_collage=getattr(self, "_use_collage", True),
                                            output_dir=output_dir,
                                            _payload=_payload)
                record["source_image"] = img_path.name
                elapsed = time.perf_counter() - _t_img
                if _tqdm:
                    bar.set_postfix_str(
                        f"{img_path.name[:30]} done {elapsed:.1f}s", refresh=True)
                else:
                    print(f" {elapsed:.1f}s")

            except Exception as exc:
                elapsed   = time.perf_counter() - _t_img
                err_msg   = f"{img_path.name}: {exc}"
                errors.append(err_msg)
                log.error("Extraction failed for %s: %s", img_path.name, exc)
                if _tqdm:
                    bar.write(f" ERROR — {err_msg}")
                else:
                    print(f"ERROR — {err_msg}")
                self.results.append({"source_image": img_path.name, "error": str(exc)})
                continue
            finally:
                if actual_path != img_path and actual_path.exists():
                    actual_path.unlink(missing_ok=True)

            taxon_raw   = record.get("scientificName", "").strip()
            taxon_clean = _clean_taxon(taxon_raw) if taxon_raw else ""
            record["taxon_clean"] = taxon_clean
            if skip_taxonomy:
                record["tax_audit"] = {"status": "SKIPPED", "accepted": "", "sources": [], "conf": 0}
            else:
                record["tax_audit"] = taxonomic_audit(taxon_clean) if taxon_clean else {
                    "status": "NOT_FOUND", "accepted": "", "sources": [], "conf": 0
                }
            record = _split_record(record)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            self.results.append(record)
            if delay > 0:
                time.sleep(delay)

        if _tqdm:
            bar.close()
        crop_pool.shutdown(wait=True)

        n_ok     = sum(1 for r in self.results if "error" not in r)
        n_errors = len(errors)
        print(f"\n   Done — {n_ok}/{n_total} extracted successfully")
        if errors:
            print(f"{n_errors} error(s):")
            for e in errors:
                print(f"      • {e}")
            print(f"\n Re-run to retry failed images (cached results are kept).")

        log.info("Audit loop done. %d ok, %d errors.", n_ok, n_errors)
