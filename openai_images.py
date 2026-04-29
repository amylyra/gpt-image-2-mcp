"""OpenAI Image API adapter (gpt-image-2).

Sole entry point for `client.images.generate` and `client.images.edit`. Centralizes:
  - aspect-ratio → size mapping (size_for_aspect)
  - size validation (multiples of 16, ratio <= 3:1, pixel range, max edge 3840)
  - parameter validation — rejects unsupported params before reaching the SDK:
      * response_format
      * thinking
      * input_fidelity
      * background="transparent"
      * output_compression with output_format="png"
  - usage and bytes extraction

Models: gpt-image-2 (default). Fallback gpt-image-1.5 (NOT deprecated gpt-image-1).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from typing import Any, Optional

from openai import OpenAI


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
FALLBACK_MODEL = "gpt-image-1.5"

# ---------------------------------------------------------------------------
# Aspect-ratio shim — preserves true ratio across default/high tiers.
# All sizes satisfy: max edge <=3840, multiple of 16, <=3:1 ratio,
#                    pixels in [655_360, 8_294_400].
# ---------------------------------------------------------------------------
ASPECT_SIZES = {
    # alias            default size      high size
    "1:1":               ("1024x1024",  "2048x2048"),
    "4:5":               ("1024x1280",  "1536x1920"),
    "5:4":               ("1280x1024",  "1920x1536"),
    "16:9":              ("2048x1152",  "3840x2160"),
    "9:16":              ("1152x2048",  "2160x3840"),
    "3:2":               ("1536x1024",  "3072x2048"),
    "2:3":               ("1024x1536",  "2048x3072"),
    "infographic-portrait":   ("1024x1536", "2048x3072"),
    "infographic-landscape":  ("1536x1024", "3072x2048"),
    "social-square":          ("1024x1024", "2048x2048"),
    "social-vertical":        ("1152x2048", "2160x3840"),
    "social-horizontal":      ("2048x1152", "3840x2160"),
}


def size_for_aspect(alias: str, *, prefer_high: bool = False) -> str:
    """Return a size string for the given alias. Tools must call this — never construct sizes manually."""
    if alias not in ASPECT_SIZES:
        raise ValueError(
            f"Unknown aspect alias '{alias}'. "
            f"Supported: {sorted(ASPECT_SIZES.keys())}"
        )
    default, high = ASPECT_SIZES[alias]
    return high if prefer_high else default


def parse_size(size: str) -> tuple[int, int]:
    try:
        w_str, h_str = size.lower().split("x")
        return int(w_str), int(h_str)
    except Exception:
        raise ValueError(f"Invalid size string '{size}'. Expected format like '1024x1024'.")


def validate_size(w: int, h: int) -> None:
    """Enforce gpt-image-2 size constraints."""
    if w <= 0 or h <= 0:
        raise ValueError(f"Size dimensions must be positive: got {w}x{h}.")
    if max(w, h) > 3840:
        raise ValueError(f"Max edge is 3840px; got {max(w, h)}.")
    pixels = w * h
    if pixels < 655_360:
        raise ValueError(
            f"Image too small: {pixels:,} pixels (min 655,360). Use a larger size."
        )
    if pixels > 8_294_400:
        raise ValueError(
            f"Image too large: {pixels:,} pixels (max 8,294,400)."
        )
    ratio = max(w, h) / min(w, h)
    if ratio > 3.0:
        raise ValueError(f"Aspect ratio must be <=3:1; got {ratio:.2f}:1.")
    if w % 16 != 0 or h % 16 != 0:
        raise ValueError(f"Width and height must be multiples of 16; got {w}x{h}.")


# ---------------------------------------------------------------------------
# Parameter validation — rejects unsupported params before SDK call.
# ---------------------------------------------------------------------------
_BANNED_KWARGS = ("response_format", "thinking", "input_fidelity")


def validate_image_params(**kwargs: Any) -> None:
    """Raise ValueError if caller is about to pass unsupported params to gpt-image-2."""
    for k in _BANNED_KWARGS:
        if k in kwargs:
            raise ValueError(
                f"Param '{k}' is not supported by gpt-image-2. "
                "Image responses are always base64; do not pass response_format. "
                "thinking/input_fidelity are not Image API params."
            )

    if kwargs.get("background") == "transparent":
        raise ValueError(
            "background='transparent' is not supported by gpt-image-2. "
            "Use output_format='png' for images with transparent regions when supported."
        )

    if kwargs.get("output_format") == "png" and kwargs.get("output_compression") is not None:
        raise ValueError(
            "output_compression is only valid with output_format='jpeg' or 'webp'."
        )

    size = kwargs.get("size")
    if size:
        w, h = parse_size(size)
        validate_size(w, h)

    n = kwargs.get("n")
    if n is not None:
        max_n = int(os.environ.get("MAX_COUNT_PER_REQUEST", 4))
        if not isinstance(n, int) or n < 1 or n > max_n:
            raise ValueError(
                f"count/n must be an integer between 1 and {max_n} (got {n!r})."
            )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_CLIENT: Optional[OpenAI] = None


def get_client() -> OpenAI:
    global _CLIENT
    if _CLIENT is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY env var is required to call the Image API."
            )
        _CLIENT = OpenAI(api_key=api_key)
    return _CLIENT


# ---------------------------------------------------------------------------
# Calls — generate, edit
# ---------------------------------------------------------------------------
async def call_generate(
    *,
    prompt: str,
    size: str,
    n: int = 1,
    quality: str = "medium",
    output_format: str = "png",
    output_compression: Optional[int] = None,
    moderation: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[list[bytes], dict]:
    """Call client.images.generate. Returns (image_bytes_list, usage_dict)."""
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "prompt": prompt,
        "size": size,
        "n": n,
        "quality": quality,
        "output_format": output_format,
    }
    if output_compression is not None and output_format in ("jpeg", "webp"):
        kwargs["output_compression"] = output_compression
    if moderation:
        kwargs["moderation"] = moderation

    validate_image_params(**kwargs)
    client = get_client()
    response = await asyncio.to_thread(client.images.generate, **kwargs)
    return extract_images(response), extract_usage(response)


async def call_edit(
    *,
    prompt: str,
    images: list[tuple[bytes, str]],
    mask: Optional[bytes] = None,
    size: str,
    n: int = 1,
    quality: str = "medium",
    output_format: str = "png",
    output_compression: Optional[int] = None,
    model: Optional[str] = None,
) -> tuple[list[bytes], dict]:
    """Call client.images.edit. Up to 16 source images.

    `images` is a list of (bytes, mime). The OpenAI SDK accepts file-like objects;
    we wrap each as a BytesIO with a synthetic filename so the SDK detects mime.
    """
    if not images:
        raise ValueError("At least one source image is required for edit.")
    if len(images) > 16:
        raise ValueError(f"At most 16 source images allowed; got {len(images)}.")

    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "prompt": prompt,
        "size": size,
        "n": n,
        "quality": quality,
        "output_format": output_format,
    }
    if output_compression is not None and output_format in ("jpeg", "webp"):
        kwargs["output_compression"] = output_compression

    validate_image_params(**kwargs)

    file_objs = []
    for i, (img_bytes, mime) in enumerate(images):
        ext = _ext_for_mime(mime)
        bio = io.BytesIO(img_bytes)
        bio.name = f"image_{i}.{ext}"
        file_objs.append(bio)

    call_kwargs = dict(kwargs)
    call_kwargs["image"] = file_objs[0] if len(file_objs) == 1 else file_objs
    if mask is not None:
        mask_bio = io.BytesIO(mask)
        mask_bio.name = "mask.png"
        call_kwargs["mask"] = mask_bio

    client = get_client()
    response = await asyncio.to_thread(client.images.edit, **call_kwargs)
    return extract_images(response), extract_usage(response)


def _ext_for_mime(mime: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(mime, "png")


# ---------------------------------------------------------------------------
# Response unpacking
# ---------------------------------------------------------------------------
def extract_images(response: Any) -> list[bytes]:
    """Decode b64_json from each entry in response.data."""
    data = getattr(response, "data", None) or []
    out: list[bytes] = []
    for entry in data:
        b64 = getattr(entry, "b64_json", None)
        if isinstance(entry, dict):
            b64 = b64 or entry.get("b64_json")
        if not b64:
            raise ValueError(
                "Image API returned an entry without b64_json. "
                "This should not happen — file a bug with the request_id."
            )
        out.append(base64.b64decode(b64))
    if not out:
        raise ValueError("Image API returned no images.")
    return out


def extract_usage(response: Any) -> dict:
    """Pull token usage off the response in a dict-or-object-tolerant way."""
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
    out = {}
    for attr in ("input_tokens", "output_tokens", "total_tokens",
                 "input_tokens_details", "output_tokens_details"):
        v = getattr(usage, attr, None)
        if v is not None:
            out[attr] = v if not hasattr(v, "model_dump") else v.model_dump()
    return out
