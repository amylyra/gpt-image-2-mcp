"""Responses API adapter — used only by refine_image (v0.4).

Why a separate module: Responses is a different API surface than Images. It uses:
  - tool_choice={"type": "image_generation"}  (forced — never optional)
  - input as a list of content items (text, input_image, etc.)
  - chaining via previous_response_id OR image_generation_call.id

Why we use it: refine_image is a multi-turn iteration loop. Responses preserves
the conversation state across calls, which gives the model the prior-turn context
without re-uploading the image every time.

Token rates differ slightly because reasoning tokens may be billed; we still
report the actual cost from response.usage in the tool response.

Default: gpt-5.5 (env: OPENAI_REFINE_MODEL). reasoning_effort defaults to "low" —
this is a tool call, not a strategy task.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Optional

from openai_images import get_client


DEFAULT_REFINE_MODEL = os.environ.get("OPENAI_REFINE_MODEL", "gpt-5.5")
DEFAULT_REASONING_EFFORT = os.environ.get("OPENAI_REFINE_REASONING_EFFORT", "low")


def _b64_data_url(img_bytes: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"


async def call_refine(
    *,
    prompt: str,
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/png",
    previous_response_id: Optional[str] = None,
    image_generation_call_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[list[bytes], dict, str]:
    """Call client.responses.create with forced image_generation tool_choice.

    Chaining (mutually exclusive — pick at most one):
      - previous_response_id: continues the conversation from a prior Responses call.
      - image_generation_call_id: refines the specific image generation call.

    If neither is given AND image_bytes is provided, the image is included as an
    input_image content block. If neither chain nor image is given, the call is a
    plain text-to-image refinement (rare — usually you want at least one).

    Returns (image_bytes_list, usage_dict, response_id).
    The response_id is what callers should pass back as previous_response_id next turn.
    """
    if previous_response_id and image_generation_call_id:
        raise ValueError(
            "Pass at most one of previous_response_id or image_generation_call_id."
        )

    client = get_client()

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if image_bytes is not None and not (previous_response_id or image_generation_call_id):
        content.append({
            "type": "input_image",
            "image_url": _b64_data_url(image_bytes, image_mime),
        })

    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_REFINE_MODEL,
        "input": [{"role": "user", "content": content}],
        # ALWAYS forced — never let this be optional.
        "tool_choice": {"type": "image_generation"},
        "tools": [{"type": "image_generation"}],
    }
    effort = reasoning_effort or DEFAULT_REASONING_EFFORT
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    if image_generation_call_id:
        # Per Responses chaining contract — point at the prior image_generation call.
        kwargs["input"][0]["content"].insert(0, {
            "type": "image_generation_call",
            "id": image_generation_call_id,
        })

    response = await asyncio.to_thread(client.responses.create, **kwargs)
    images = _extract_images_from_response(response)
    usage = _extract_usage(response)
    response_id = getattr(response, "id", None) or ""
    return images, usage, response_id


def _extract_images_from_response(response: Any) -> list[bytes]:
    """Pull base64 PNGs out of a Responses API response.

    Responses output is a list of items; image generation tool calls land as
    items of type "image_generation_call" with a `result` (b64) field. Some
    SDK shapes nest under `output` instead of `data` — handle both.
    """
    items = getattr(response, "output", None) or getattr(response, "data", None) or []
    out: list[bytes] = []
    for item in items:
        item_type = getattr(item, "type", None) or (isinstance(item, dict) and item.get("type"))
        if item_type != "image_generation_call":
            continue
        b64 = getattr(item, "result", None) or (isinstance(item, dict) and item.get("result"))
        if not b64:
            b64 = getattr(item, "b64_json", None) or (isinstance(item, dict) and item.get("b64_json"))
        if b64:
            out.append(base64.b64decode(b64))
    if not out:
        raise ValueError(
            "Refine response contained no image_generation_call results. "
            "Check that tool_choice is forced and the model supports image generation."
        )
    return out


def _extract_usage(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if hasattr(usage, "to_dict"):
        return usage.to_dict()
    if isinstance(usage, dict):
        return dict(usage)
    return {}


def extract_image_generation_call_id(response: Any) -> Optional[str]:
    """Find the id of the image_generation_call item — usable as a chain handle."""
    items = getattr(response, "output", None) or getattr(response, "data", None) or []
    for item in items:
        item_type = getattr(item, "type", None) or (isinstance(item, dict) and item.get("type"))
        if item_type == "image_generation_call":
            return getattr(item, "id", None) or (isinstance(item, dict) and item.get("id"))
    return None
