"""Infographic layout templates + the create_infographic pipeline.

Hybrid pipeline:
  1. Choose a layout template (slot positions for text + a free region for background).
  2. Ask gpt-image-2 to generate ONLY the background, with negative-space hints in
     the prompt corresponding to the template's free region. Text is NOT requested
     of the model — it's all rendered deterministically afterwards.
  3. Composite the deterministic text blocks (title, subtitle, bullets, footer)
     onto the AI-generated background using typography.py.

This keeps text accuracy at 100% while letting gpt-image-2 do what it's best at
(brand-consistent backgrounds and visual style).

Templates:
  - portrait-stat        2:3 portrait. Big stat number + label. Background fills.
  - landscape-comparison 3:2 landscape. Two-column compare list. Bg around them.
  - square-list          1:1. Title + 3–6 bullets, top-aligned. Bg below.
"""

from __future__ import annotations

from typing import Optional

from openai_images import call_generate, size_for_aspect


# Slot layout: bbox in [0,1] coordinates over the canvas.
# text_align is the default for that slot.
# negative_space is a phrase the prompt builder uses to ask the AI to leave room.
LAYOUTS = {
    "portrait-stat": {
        "aspect_ratio": "infographic-portrait",
        "negative_space": "leaves the bottom 60% of the frame as a calm, low-detail area suitable for large overlaid statistics and a label",
        "slots": {
            "stat":     {"bbox": {"x": 0.06, "y": 0.42, "w": 0.88, "h": 0.30}, "align": "center"},
            "subtitle": {"bbox": {"x": 0.10, "y": 0.74, "w": 0.80, "h": 0.10}, "align": "center"},
            "footer":   {"bbox": {"x": 0.10, "y": 0.90, "w": 0.80, "h": 0.06}, "align": "center"},
        },
        "default_styles": {"stat": "stat", "subtitle": "subtitle", "footer": "footer"},
    },
    "landscape-comparison": {
        "aspect_ratio": "infographic-landscape",
        "negative_space": "leaves a clean horizontal band across the upper third for a title and two clean vertical columns below for comparison bullets",
        "slots": {
            "title":      {"bbox": {"x": 0.05, "y": 0.05, "w": 0.90, "h": 0.18}, "align": "center"},
            "left":       {"bbox": {"x": 0.06, "y": 0.28, "w": 0.42, "h": 0.66}, "align": "left"},
            "right":      {"bbox": {"x": 0.52, "y": 0.28, "w": 0.42, "h": 0.66}, "align": "left"},
        },
        "default_styles": {"title": "title", "left": "body", "right": "body"},
    },
    "square-list": {
        "aspect_ratio": "social-square",
        "negative_space": "leaves the upper 60% of the frame as a calm, low-detail area suitable for a title and short bullet list",
        "slots": {
            "title":    {"bbox": {"x": 0.06, "y": 0.06, "w": 0.88, "h": 0.18}, "align": "left"},
            "subtitle": {"bbox": {"x": 0.06, "y": 0.26, "w": 0.88, "h": 0.10}, "align": "left"},
            "bullets":  {"bbox": {"x": 0.06, "y": 0.40, "w": 0.88, "h": 0.50}, "align": "left"},
            "footer":   {"bbox": {"x": 0.06, "y": 0.92, "w": 0.88, "h": 0.06}, "align": "left"},
        },
        "default_styles": {"title": "title", "subtitle": "subtitle", "bullets": "bullet", "footer": "footer"},
    },
}


def list_layouts() -> list[str]:
    return sorted(LAYOUTS.keys())


def _build_background_prompt(user_prompt: str, layout_key: str) -> str:
    layout = LAYOUTS[layout_key]
    return (
        f"{user_prompt.strip()}. "
        f"Compose this image so it {layout['negative_space']}. "
        "Do NOT render any text, words, numbers, or letters in the image — "
        "the text will be added separately. "
        "Style: clean, brand-consistent, high contrast where text will sit."
    )


async def generate_infographic_background(
    *, prompt: str, layout: str, quality: str = "medium",
) -> tuple[bytes, dict]:
    """Generate the background image only (no text). Returns (bytes, usage)."""
    if layout not in LAYOUTS:
        raise ValueError(f"Unknown layout '{layout}'. One of: {list_layouts()}.")
    cfg = LAYOUTS[layout]
    size = size_for_aspect(cfg["aspect_ratio"], prefer_high=(quality == "high"))
    bg_prompt = _build_background_prompt(prompt, layout)
    images, usage = await call_generate(
        prompt=bg_prompt, size=size, n=1, quality=quality, output_format="png",
    )
    return images[0], usage


def composite_infographic_text(
    background_bytes: bytes,
    *,
    layout: str,
    content: dict,
    color: Optional[str] = None,
    background_color_for_slots: Optional[str] = None,
) -> bytes:
    """Composite the deterministic text layer onto the AI-generated background.

    content: a dict mapping slot name -> str OR list[str] (for bullets / multi-block).
             Slot names depend on the layout (title, subtitle, stat, bullets, footer,
             left, right).
    """
    if layout not in LAYOUTS:
        raise ValueError(f"Unknown layout '{layout}'.")
    from typography import composite_typography_block

    cfg = LAYOUTS[layout]
    canvas_bytes = background_bytes
    for slot_name, slot in cfg["slots"].items():
        value = content.get(slot_name)
        if value is None:
            continue
        style = cfg["default_styles"].get(slot_name, "body")
        items = value if isinstance(value, list) else [value]
        blocks = [{"text": s, "style": style, "color": color} for s in items if s]
        if not blocks:
            continue
        canvas_bytes = composite_typography_block(
            canvas_bytes,
            blocks,
            bbox=slot["bbox"],
            align=slot.get("align", "left"),
            background_color=background_color_for_slots,
        )
    return canvas_bytes
