"""Cost estimation + usage-based actuals.

Token rates (per 1M tokens) used in v0.1:
  - image input:        $8.00
  - cached image input: $2.00
  - image output:       $30.00
  - text input:         $5.00
  - cached text input:  $1.25

`estimate_cost(...)` is a pre-call rough number based on size/quality.
`actual_cost_from_usage(usage)` is authoritative, computed from response.usage.
Both attach to every tool response under `estimated_cost_usd` / `actual_cost_usd`.

Deterministic-mode renders (text overlay / CTA) report 0.0 actual cost.
"""

from __future__ import annotations

from typing import Optional


# Per 1M tokens
RATE_IMAGE_INPUT_PER_MTOK = 8.00
RATE_IMAGE_INPUT_CACHED_PER_MTOK = 2.00
RATE_IMAGE_OUTPUT_PER_MTOK = 30.00
RATE_TEXT_INPUT_PER_MTOK = 5.00
RATE_TEXT_INPUT_CACHED_PER_MTOK = 1.25


# Approx output tokens per quality tier — used pre-call for an estimate.
# These are rough and the post-call actuals replace them in the response.
_OUTPUT_TOKEN_ESTIMATE = {
    "low":    300,
    "medium": 1100,
    "high":   3500,
}


def estimate_cost(
    *,
    quality: str = "medium",
    n: int = 1,
    has_image_input: bool = False,
    prompt_chars: int = 0,
) -> float:
    """Return a rough USD estimate before the API call."""
    out_per_image = _OUTPUT_TOKEN_ESTIMATE.get(quality, _OUTPUT_TOKEN_ESTIMATE["medium"])
    output_cost = (out_per_image * n) * RATE_IMAGE_OUTPUT_PER_MTOK / 1_000_000
    text_input_tokens = max(50, prompt_chars // 4)
    text_cost = text_input_tokens * RATE_TEXT_INPUT_PER_MTOK / 1_000_000
    image_input_cost = 1500 * RATE_IMAGE_INPUT_PER_MTOK / 1_000_000 if has_image_input else 0.0
    return round(output_cost + text_cost + image_input_cost, 4)


def actual_cost_from_usage(usage: dict | None) -> Optional[float]:
    """Compute USD cost from a usage dict (response.usage). Returns None if unknown."""
    if not usage:
        return None
    in_text = _get(usage, "input_tokens_details", "text_tokens", default=0)
    in_text_cached = _get(usage, "input_tokens_details", "cached_text_tokens", default=0)
    in_image = _get(usage, "input_tokens_details", "image_tokens", default=0)
    in_image_cached = _get(usage, "input_tokens_details", "cached_image_tokens", default=0)
    out_image = _get(usage, "output_tokens_details", "image_tokens", default=0)

    if not any([in_text, in_image, out_image, in_text_cached, in_image_cached]):
        total_in = usage.get("input_tokens", 0) or 0
        total_out = usage.get("output_tokens", 0) or 0
        if not (total_in or total_out):
            return None
        return round(
            total_in * RATE_TEXT_INPUT_PER_MTOK / 1_000_000
            + total_out * RATE_IMAGE_OUTPUT_PER_MTOK / 1_000_000,
            4,
        )

    cost = (
        in_text * RATE_TEXT_INPUT_PER_MTOK / 1_000_000
        + in_text_cached * RATE_TEXT_INPUT_CACHED_PER_MTOK / 1_000_000
        + in_image * RATE_IMAGE_INPUT_PER_MTOK / 1_000_000
        + in_image_cached * RATE_IMAGE_INPUT_CACHED_PER_MTOK / 1_000_000
        + out_image * RATE_IMAGE_OUTPUT_PER_MTOK / 1_000_000
    )
    return round(cost, 4)


def _get(d: dict, key: str, subkey: str | None = None, default=0):
    v = d.get(key, default if subkey is None else {})
    if subkey is None:
        return v
    if isinstance(v, dict):
        return v.get(subkey, default)
    inner = getattr(v, subkey, None)
    return inner if inner is not None else default


def attach_cost_meta(
    usage: dict,
    *,
    quality: str,
    n: int,
    has_image_input: bool,
    prompt_chars: int,
    attempt_count: int,
    validation_status: str = "ok",
) -> dict:
    """Build the cost block included in every tool response."""
    return {
        "usage": usage or {},
        "estimated_cost_usd": estimate_cost(
            quality=quality, n=n, has_image_input=has_image_input, prompt_chars=prompt_chars,
        ),
        "actual_cost_usd": actual_cost_from_usage(usage),
        "attempt_count": attempt_count,
        "validation_status": validation_status,
    }
