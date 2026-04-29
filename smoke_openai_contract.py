"""Cheap live smoke test against the real OpenAI Image API.

Run: python smoke_openai_contract.py --cheap

What it checks:
  1. generate_image with quality=low, 1024x1024 returns valid PNG bytes.
  2. edit_image with a tiny source returns valid PNG bytes.
  3. The SDK is never called with: response_format, thinking, input_fidelity,
     background='transparent', or output_compression with output_format='png'.
  4. Cost report — fails if total > $0.10 (override with --budget=0.20).

Skipped automatically if OPENAI_API_KEY is unset.
Designed to cost ~$0.02–$0.05 per run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from io import BytesIO
from typing import Any

from PIL import Image as PILImage


def _tiny_png() -> bytes:
    img = PILImage.new("RGB", (256, 256), (200, 220, 240))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _is_png(b: bytes) -> bool:
    return len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Wrap the OpenAI client to capture every kwarg the SDK is called with.
# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record(self, label: str, **kwargs):
        self.calls.append((label, kwargs))


def _patch_client(recorder: _Recorder):
    """Monkey-patch openai_images.get_client to wrap method calls."""
    import openai_images

    original_get_client = openai_images.get_client
    real_client = original_get_client()

    real_generate = real_client.images.generate
    real_edit = real_client.images.edit

    def wrapped_generate(**kwargs):
        recorder.record("generate", **kwargs)
        return real_generate(**kwargs)

    def wrapped_edit(**kwargs):
        recorder.record("edit", **kwargs)
        return real_edit(**kwargs)

    real_client.images.generate = wrapped_generate
    real_client.images.edit = wrapped_edit


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
BANNED_KWARGS = ("response_format", "thinking", "input_fidelity")


def assert_no_unsupported(recorder: _Recorder) -> None:
    bad = []
    for label, kwargs in recorder.calls:
        for k in BANNED_KWARGS:
            if k in kwargs:
                bad.append(f"{label}() called with banned kwarg: {k}")
        if kwargs.get("background") == "transparent":
            bad.append(f"{label}() called with background='transparent'")
        if kwargs.get("output_format") == "png" and kwargs.get("output_compression") is not None:
            bad.append(f"{label}() called with output_compression on PNG output")
    if bad:
        for b in bad:
            print(f"  FAIL: {b}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def amain(args: argparse.Namespace) -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set — skipping live smoke.")
        return 0

    os.environ.setdefault("IMAGE_URL_SIGNING_KEY", "0" * 64)

    from costs import actual_cost_from_usage
    from openai_images import call_edit, call_generate

    recorder = _Recorder()
    _patch_client(recorder)

    total_cost = 0.0
    started = time.time()

    print("[1/2] generate_image — quality=low, 1024x1024")
    images, usage = await call_generate(
        prompt="A flat-illustration cat with a tiny scarf.",
        size="1024x1024", n=1, quality="low", output_format="png",
    )
    assert images and _is_png(images[0]), "generate did not return valid PNG"
    c = actual_cost_from_usage(usage) or 0.0
    total_cost += c
    print(f"      ok — {len(images[0]):,}B PNG, cost ~${c:.4f}")

    print("[2/2] edit_image — quality=low, 1024x1024, single source")
    src = _tiny_png()
    images, usage = await call_edit(
        prompt="Add a small star in the upper-right corner.",
        images=[(src, "image/png")],
        size="1024x1024", n=1, quality="low", output_format="png",
    )
    assert images and _is_png(images[0]), "edit did not return valid PNG"
    c = actual_cost_from_usage(usage) or 0.0
    total_cost += c
    print(f"      ok — {len(images[0]):,}B PNG, cost ~${c:.4f}")

    print(f"\nTotal cost: ${total_cost:.4f} (budget ${args.budget})")
    print(f"Total time: {time.time() - started:.1f}s")

    print("\n[contract] checking SDK call args for banned params...")
    assert_no_unsupported(recorder)
    print(f"      ok — {len(recorder.calls)} call(s), no banned params")

    if total_cost > args.budget:
        print(f"\nFAIL: cost ${total_cost:.4f} exceeded budget ${args.budget}.")
        return 3

    print("\nAll smoke checks passed.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cheap", action="store_true",
                    help="Run cheap path (default behavior — no other modes yet).")
    ap.add_argument("--budget", type=float, default=0.10,
                    help="Max total cost USD; fail if exceeded (default 0.10).")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
