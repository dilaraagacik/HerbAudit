"""
Extraction-cost calculation (LiteLLM pricing lookup with a local fallback
table) and small OpenAI/Gemini batch-response helpers.
"""
from __future__ import annotations

import logging

log = logging.getLogger("herbaudit")


def _lazy_imports():
    global litellm
    import litellm

litellm = None


MODEL_COSTS: dict[str, dict[str, float]] = {
    "gemini-3.5-flash-lite":  {"in": 0.30,  "out": 2.50},  # standard tier, global
    "gemini-2.5-flash":       {"in": 0.30,  "out": 2.50},
    "gemini-2.5-pro":         {"in": 1.25,  "out": 10.00},
    "gemini-2.5-flash-lite":  {"in": 0.10,  "out": 0.40},
    "gemini-2.0-flash":       {"in": 0.10,  "out": 0.40},
    "gemini-1.5-flash":       {"in": 0.075, "out": 0.30},
    "gemini-1.5-pro":         {"in": 1.25,  "out": 5.00},
    "gpt-4o":                 {"in": 2.50,  "out": 10.00},
    "gpt-4o-mini":            {"in": 0.15,  "out": 0.60},
    "o4-mini":                {"in": 1.10,  "out": 4.40},
}


# Gemini's Batch API bills at half the standard per-token rate in exchange for
# async (up-to-24h target, usually much quicker) turnaround.
_BATCH_COST_DISCOUNT = 0.5


def _calculate_cost(model: str, input_tokens: int, output_tokens: int,
                     provider: str = "gemini") -> float:
    """
    Cost via LiteLLM's pricing registry when it recognizes the model (Gemini
    models are "gemini/"-prefixed for LiteLLM; OpenAI names used bare). Falls
    back to the local MODEL_COSTS table for anything LiteLLM doesn't recognize.
    """
    litellm_model = f"gemini/{model}" if provider == "gemini" else model
    try:
        in_cost, out_cost = litellm.cost_per_token(
            model=litellm_model, prompt_tokens=input_tokens, completion_tokens=output_tokens,
        )
        cost = in_cost + out_cost
        log.info(
            "Cost via LiteLLM: model=%s in_tok=%d out_tok=%d in_cost=$%.6f out_cost=$%.6f total=$%.6f",
            model, input_tokens, output_tokens, in_cost, out_cost, cost,
        )
    except Exception as exc:
        log.warning("LiteLLM did not recognize model '%s' (%s) — checking local rate table", model, exc)
        # Exact match only — a prefix match risks billing one model at another's rate.
        rates = MODEL_COSTS.get(model)
        if rates is None:
            raise ValueError(
                f"No pricing data for model '{model}' — not recognized by LiteLLM and no entry in "
                f"MODEL_COSTS. Add one to MODEL_COSTS in functions.py, or update litellm."
            ) from exc
        cost = (input_tokens * rates["in"] + output_tokens * rates["out"]) / 1_000_000
        log.info(
            "Cost via local table: model=%s in_tok=%d out_tok=%d rate_in=$%.2f/1M rate_out=$%.2f/1M total=$%.6f",
            model, input_tokens, output_tokens, rates["in"], rates["out"], cost,
        )
    return round(cost, 6)


def _openai_token_kwarg(model: str, n: int) -> dict:
    """
    o-series reasoning models (o1/o3/o4) and the gpt-5 family reject
    `max_tokens` and require `max_completion_tokens` instead. Older
    non-reasoning chat models still expect `max_tokens`.
    """
    if model.startswith(("o1", "o3", "o4", "gpt-5")):
        return {"max_completion_tokens": n}
    return {"max_tokens": n}


def _extract_text_from_batch_response(resp: dict) -> str:
    """Pull the model's text out of a batch result line's "response" object
    (candidates -> content -> parts -> text), the raw-JSON equivalent of a
    live call's resp.text."""
    candidates = resp.get("candidates") or []
    if not candidates:
        raise ValueError("batch response has no candidates")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if p.get("text"))
    if not text:
        raise ValueError("batch response candidate has no text part")
    return text
