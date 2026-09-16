import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

from herbaudit.audit.run import run_audit
from herbaudit.pipeline.weights import _ensure_default_archival_weights

try:
    import tomllib          # stdlib on Python 3.11+
except ImportError:
    import tomli as tomllib # backport for 3.8-3.10 (see requirements.txt)

# Load API keys from .env into os.environ (checks ./.env and ~/.herbaudit/.env;
# never overrides a variable already exported in the shell).
load_dotenv()
load_dotenv(Path.home() / ".herbaudit" / ".env")

# Auto-downloaded on first use if missing (see _ensure_default_archival_weights);
# overridable via --weights or HERBAUDIT_WEIGHTS.
DEFAULT_WEIGHTS = Path.home() / ".herbaudit" / "models" / "archival_detector_best.pt"

# Auto-loaded if present; override with --config or HERBAUDIT_CONFIG.
# Secrets (API keys) deliberately stay CLI/env-only, not config-file material.
DEFAULT_CONFIG_PATH = Path.home() / ".herbaudit" / "config.toml"


def _load_config(explicit_path: str | None) -> dict:
    if explicit_path:
        path = Path(explicit_path)
    elif os.environ.get("HERBAUDIT_CONFIG"):
        path = Path(os.environ["HERBAUDIT_CONFIG"])
    else:
        path = DEFAULT_CONFIG_PATH

    if not path.exists():
        if explicit_path:
            print(f"Config file not found: {path}", file=sys.stderr)
        return {}
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
        print(f"  Config: {path}")
        return cfg
    except Exception as exc:
        print(f"Could not read config file {path} ({exc}) — ignoring", file=sys.stderr)
        return {}


def _infer_provider(model_name: str) -> str:
    """
    Which provider a --model name belongs to. Gemini and OpenAI names follow
    fixed, documented prefixes; anything else is assumed to be a local Ollama
    model, since Ollama names are user-defined and have no fixed pattern —
    that's also why Ollama is the fallback rather than requiring a prefix.
    """
    m = model_name.lower()
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-")):
        return "openai"
    return "ollama"

_G  = "\033[38;5;83m"    # bright green  — flags
_DG = "\033[38;5;65m"    # dim green     — descriptions
_Y  = "\033[38;5;228m"   # yellow        — values / defaults
_W  = "\033[38;5;252m"   # white         — normal text
_B  = "\033[1m"          # bold
_R  = "\033[38;5;203m"   # red/orange    — required badge
_S  = "\033[38;5;240m"   # dim grey      — section separators
_C  = "\033[38;5;108m"   # muted green   — choices
_X  = "\033[0m"          # reset


def _print_help():
    W = 72  # total width

    def section(title):
        bar = _S + "─" * (W - len(title) - 2) + _X
        print(f"\n{_S}{_B}{title}{_X} {bar}")

    def row(flag, meta, desc, default=None, required=False, choices=None):
        req_tag = f" {_R}[required]{_X}" if required else ""
        flag_str = f"  {_G}{_B}{flag}{_X}"
        if meta:
            flag_str += f" {_DG}{meta}{_X}"
        flag_str += req_tag

        # Pad flag column to 30 chars (visible chars only)
        visible_len = len(flag) + (len(meta) + 1 if meta else 0) + (10 if required else 0)
        pad = max(1, 32 - visible_len)
        print(flag_str + " " * pad, end="")

        # Description
        desc_str = f"{_W}{desc}{_X}"
        if default:
            desc_str += f"  {_Y}[{default}]{_X}"
        if choices:
            desc_str += "  " + "  ".join(f"{_C}{c}{_X}" for c in choices)
        print(desc_str)

    print()
    print(f"  {_G}{_B}HerbAudit{_X}  {_DG}AI-powered herbarium specimen transcription and validation{_X}")
    print()
    print(f"  {_W}Usage:{_X}  {_G}herbaudit{_X} {_Y}--input{_X} {_DG}<path>{_X}"
          f"  {_S}[options]{_X}")
    print(f"  {_DG}Config: ~/.herbaudit/config.toml is auto-loaded if present "
          f"(--config / HERBAUDIT_CONFIG to override) — CLI flags always win.{_X}")

    section("Required")
    row("--input", "PATH", "Folder of images  or  a CSV / Excel extraction file",
        required=True)
    row("--config", "PATH", "Config file to load instead of the default ~/.herbaudit/config.toml ")

    section("Model")
    row("--model", "NAME", "Model to use, any provider — e.g. gemini-2.5-pro, gpt-4o-mini",
        default="gemini-3.5-flash-lite")
    row("--gemini-key",   "KEY",   "Gemini API key  (or set GEMINI_API_KEY env var)")
    row("--openai-key",   "KEY",   "OpenAI API key   (or set OPENAI_API_KEY env var)")
    row("--ollama-host",  "URL",   "Ollama server address",      default="http://localhost:11434")

    section("Report")
    row("--no-reference", "", "Skip GBIF lookup — standalone extraction report with WFO check")
    row("--annotations", "PATH", "Manual ground-truth JSON for specimens with no GBIF-published "
        "occurrence — scored against this instead of skipped as unmatched. See "
        "herbaudit/files/manual_annotations.json for the format",
        default="herbaudit/files/manual_annotations.json")
    # TODO: --ocr row hidden until herbaudit.ocr_evaluator is ported into this package.
    row("--output", "NAME", "Label this run's output files")
    row("--force", "", "Re-run every image even if ./herbaudit_output/<name>_audit.json already exists")

    section("Image processing")
    row("--weights",  "PATH", "Label-region detector weights  any YOLO-family .pt checkpoint ")
    row("--detector",       "",    "auto = use --weights if set (else OpenCV); opencv = force OpenCV contours",
        choices=["auto", "opencv"],
        default="auto")
    row("--no-collage",     "",    "Skip label cropping — send full image to the LLM")

    section("Batch")
    row("--batch", "", "Use Gemini's Batch API instead of live calls — 50% of the per-token ")

    section("Logging")
    row("--verbose", "", "Show detailed per-region detection logs (INFO level). ")

    section("Example")
    print(f"  {_DG}${_X} {_G}herbaudit{_X}"
          f"  {_Y}--input{_X} ./images"
          f"  {_Y}--model{_X} gemini-2.5-pro"
          f"  {_Y}--no-reference{_X}")
    print()


class _HelpAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        _print_help()
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-h", "--help", nargs=0, action=_HelpAction)

    parser.add_argument("--input",          required=True)
    parser.add_argument("--config",         default=None, metavar="PATH")
    parser.add_argument("--model",          default=None, metavar="NAME",
                        help="One flag for any provider's model — e.g. gemini-2.5-pro, "
                             "gpt-4o-mini, qwen3-vl. Provider is inferred from the name; "
                             "still needs the matching API key (env var or --gemini-key/"
                             "--openai-key) unless it's a local Ollama model.")
    parser.add_argument("--gemini-key",     default=None)
    parser.add_argument("--openai-key",     default=None)
    parser.add_argument("--ollama-host",    default=None)
    parser.add_argument("--no-reference",   action="store_true", default=False)
    parser.add_argument("--annotations",    default=None, metavar="PATH",
                        help="Manual ground-truth JSON for specimens with no GBIF-published "
                             "occurrence (see herbaudit/files/manual_annotations.json for the "
                             "format). Left off: uses that packaged file automatically, regardless "
                             "of cwd. Pass \"\" to disable the fallback entirely.")
    # TODO: --ocr mode hidden until herbaudit.ocr_evaluator is ported.
    # parser.add_argument("--ocr",            default=None, metavar="GT_PATH")
    # parser.add_argument("--iou-threshold",  type=float, default=0.5)
    # parser.add_argument("--image-dir",      default="")
    parser.add_argument("--output",         default=None, metavar="NAME",
                        help="Label for this run's output files, so comparing "
                             "models/settings doesn't overwrite the last run. "
                             "Writes results_NAME.html/.xlsx (or "
                             "results_noreference_NAME.html/.xlsx with "
                             "--no-reference) instead of results.html/.xlsx. "
                             "Left off: plain results.html/.xlsx, as before.")
    parser.add_argument("--force",          action="store_true", default=False,
                        help="Re-run every image even if a cached "
                             "./herbaudit_output/<name>_audit.json exists — a cache entry from a "
                             "different --model is auto-detected and re-run anyway, but "
                             "--no-collage changes aren't tracked, so switching "
                             "it without --force silently replays the old result.")
    parser.add_argument("--weights",        default=None, metavar="PATH")
    # Legacy aliases for --weights — not listed in _print_help() above.
    parser.add_argument("--yolo-weights",   default=None)
    parser.add_argument("--ultralytics-weights", default=None)
    parser.add_argument("--no-collage",     action="store_true", default=False)
    parser.add_argument("--detector",       default=None,
                        choices=["auto", "opencv"])
    parser.add_argument("--batch",          action="store_true", default=False)
    parser.add_argument("--verbose",        action="store_true", default=False)

    # Show styled help if no args given
    if len(sys.argv) == 1:
        _print_help()
        sys.exit(0)

    args = parser.parse_args()

    # --verbose also unlocks the vendored YOLOv5 code's own INFO logging, which
    # reads YOLOv5_VERBOSE at import time — must be set before detection runs.
    os.environ["YOLOv5_VERBOSE"] = "true" if args.verbose else "false"
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s", force=True)

    # TODO: OCR-vs-ground-truth mode hidden until herbaudit.ocr_evaluator is ported.
    #
    # if args.ocr:
    #     from herbaudit.ocr_evaluator import evaluate_dataset, generate_html_report
    #     import json as _json
    #
    #     out_base = args.output or Path(args.input.rstrip("/\\")).name or "ocr_eval"
    #     out_json = f"{out_base}_results.json"
    #     out_html = f"{out_base}_report.html"
    #
    #     results = evaluate_dataset(args.ocr, args.input, args.iou_threshold, verbose=True,
    #                                image_dir=args.image_dir)
    #     if results:
    #         with open(out_json, "w", encoding="utf-8") as f:
    #             _json.dump(results, f, indent=2, default=str)
    #         generate_html_report(results, out_html, image_dir=args.image_dir)
    #         print(f"Done — {out_html} generated")
    #     return

    # Config file fills in defaults for anything not passed on the CLI;
    # secrets (API keys) are never read from it — CLI/env only.
    config = _load_config(args.config)

    def _cfg(section, key, cli_value, default=None):
        if cli_value is not None:
            return cli_value
        val = config.get(section, {}).get(key)
        return val if val is not None else default

    def _cfg_bool(section, key, cli_flag_present):
        if cli_flag_present:
            return True
        val = config.get(section, {}).get(key)
        return bool(val) if val is not None else False

    # Infer which provider --model's name belongs to; falls back to Gemini's default.
    model_name     = _cfg("model", "model", args.model, "gemini-3.5-flash-lite")
    model_provider = _infer_provider(model_name)

    gemini_model = model_name if model_provider == "gemini" else "gemini-3.5-flash-lite"
    openai_model = model_name if model_provider == "openai" else "gpt-4o-mini"
    ollama_model = model_name if model_provider == "ollama" else None
    ollama_host  = _cfg("model", "ollama_host", args.ollama_host, "http://localhost:11434")
    # Fixed — not user-configurable via CLI or config file.
    media_resolution = "high"

    no_reference   = _cfg_bool("report", "no_reference", args.no_reference)

    detector       = _cfg("image", "detector", args.detector, "auto")
    no_collage     = _cfg_bool("image", "no_collage", args.no_collage)
    # Fixed — not user-configurable via CLI or config file.
    max_resolution_raw = "1536"

    batch = _cfg_bool("batch", "enabled", args.batch)

    gemini_key   = args.gemini_key or os.environ.get("GEMINI_API_KEY")
    openai_key   = args.openai_key or os.environ.get("OPENAI_API_KEY")
    # resolve_weights_backend() figures out which checkpoint format this is, so
    # nothing here needs to know. --yolo-weights/--ultralytics-weights are legacy
    # aliases for --weights/HERBAUDIT_WEIGHTS.
    def _auto_download_weights():
        # Skip if the user explicitly asked to skip the detector model (--detector opencv).
        if detector == "opencv":
            return None
        path = _ensure_default_archival_weights(DEFAULT_WEIGHTS)
        return str(path) if path else None

    weights = (
        args.weights or args.yolo_weights or args.ultralytics_weights
        or os.environ.get("HERBAUDIT_WEIGHTS")
        or os.environ.get("HERBAUDIT_YOLO_WEIGHTS") or os.environ.get("HERBAUDIT_ULTRALYTICS_WEIGHTS")
        or config.get("image", {}).get("weights")
        or config.get("image", {}).get("yolo_weights") or config.get("image", {}).get("ultralytics_weights")
        or (str(DEFAULT_WEIGHTS) if DEFAULT_WEIGHTS.exists() else None)
        or _auto_download_weights()
    )

    if args.openai_key and not args.gemini_key:
        gemini_key = None
    # An explicit --model picks the provider authoritatively — clear the other
    # provider's key when it only came from an ambient env var, not an explicit
    # --gemini-key/--openai-key flag this run. Otherwise "--model gpt-4o-mini"
    # would silently still run on Gemini whenever GEMINI_API_KEY happens to be set.
    if args.model:
        if model_provider != "gemini" and not args.gemini_key:
            gemini_key = None
        if model_provider != "openai" and not args.openai_key:
            openai_key = None

    def _parse_res(raw):
        raw = raw.lower().strip()
        if "x" in raw:
            parts = raw.split("x", 1)
            return (int(parts[0]), int(parts[1]))
        return (int(raw), int(raw))

    max_res = _parse_res(max_resolution_raw)

    # --output labels this run's output files so comparing runs doesn't overwrite the last one.
    output_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", args.output).strip("_") if args.output else None

    t_start = time.perf_counter()

    run_audit(
        input_path     = args.input,
        gemini_api_key   = gemini_key,
        gemini_model     = gemini_model,
        media_resolution = media_resolution,
        openai_api_key = openai_key,
        openai_model   = openai_model,
        ollama_model   = ollama_model,
        ollama_host    = ollama_host,
        weights        = weights,
        use_collage    = not no_collage,
        detector       = detector,
        noreference    = no_reference,
        max_resolution = max_res,
        output_tag     = output_tag,
        batch          = batch,
        force          = args.force,
        annotations_path = args.annotations,
        # manual_transcriptions_path left unset — run_audit() auto-discovers the
        # packaged label_studio_annotations.json and uses it when a specimen matches.
    )

    elapsed    = time.perf_counter() - t_start
    mins, sec  = divmod(int(elapsed), 60)
    hrs,  mins = divmod(mins, 60)
    pretty     = (f"{hrs}h {mins}m {sec}s" if hrs else
                  f"{mins}m {sec}s"         if mins else
                  f"{elapsed:.1f}s")
    print(f"\n⏱  Total run time: {pretty}")


if __name__ == "__main__":
    main()
