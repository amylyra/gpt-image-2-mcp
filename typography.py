"""Multi-line typography for infographics — pure Pillow.

Why not SVG/cairo: the v5 plan called for an SVG renderer, but cairosvg/skia add
~20MB of native deps for what we actually need (multi-line text blocks with
proper line height, alignment, and hierarchical typography). Pillow's text APIs
are sufficient when wrapped properly.

Public surface:
  - render_text_blocks(canvas, blocks, *, x, y, max_width, align) -> Image
      blocks: list of {text, style ("title"|"subtitle"|"body"|"bullet"|"footer"),
                       color (optional)}
  - measure_blocks(blocks, *, max_width) -> (width, height) — used by infographic
                                                              layout to pick fonts
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from deterministic_render import _load_font


# Style configs — sizes are fractions of canvas min-edge; line-height is multiple of size.
TYPO_STYLES = {
    "title":    {"weight": "bold",    "size_frac": 0.085, "line_height": 1.10, "after": 0.30},
    "subtitle": {"weight": "regular", "size_frac": 0.045, "line_height": 1.25, "after": 0.50},
    "body":     {"weight": "regular", "size_frac": 0.030, "line_height": 1.40, "after": 0.30},
    "bullet":   {"weight": "regular", "size_frac": 0.030, "line_height": 1.40, "after": 0.20, "marker": "•  "},
    "stat":     {"weight": "bold",    "size_frac": 0.140, "line_height": 1.00, "after": 0.20},
    "footer":   {"weight": "regular", "size_frac": 0.022, "line_height": 1.30, "after": 0.0},
}


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    if max_width <= 0 or not text:
        return [text or ""]
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = current + " " + w
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def _font_for_block(block: dict, canvas_min_edge: int) -> tuple[ImageFont.FreeTypeFont, dict]:
    style_name = block.get("style", "body")
    cfg = TYPO_STYLES.get(style_name, TYPO_STYLES["body"])
    size = max(11, int(canvas_min_edge * cfg["size_frac"]))
    font = _load_font(cfg["weight"], size)
    return font, cfg


def measure_blocks(
    blocks: list[dict], *, max_width: int, canvas_min_edge: int
) -> tuple[int, int]:
    """Return (used_width, total_height) for a sequence of text blocks."""
    if not blocks:
        return 0, 0
    canvas = Image.new("RGBA", (max_width, 8000), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    total_h = 0
    used_w = 0
    for block in blocks:
        font, cfg = _font_for_block(block, canvas_min_edge)
        text = block.get("text", "") or ""
        marker = cfg.get("marker", "")
        prefix_w = draw.textbbox((0, 0), marker, font=font)[2] if marker else 0
        lines = _wrap(draw, text, font, max(max_width - prefix_w, 1))
        line_h = int(font.size * cfg["line_height"])
        total_h += line_h * len(lines) + int(font.size * cfg["after"])
        for line in lines:
            w = draw.textbbox((0, 0), line, font=font)[2] + prefix_w
            if w > used_w:
                used_w = w
    return min(used_w, max_width), total_h


def render_text_blocks(
    canvas: Image.Image,
    blocks: list[dict],
    *,
    x: int,
    y: int,
    max_width: int,
    align: str = "left",
) -> Image.Image:
    """Composite a sequence of text blocks onto canvas at (x, y). Returns the canvas."""
    draw = ImageDraw.Draw(canvas)
    canvas_min_edge = min(canvas.size)
    cursor_y = y
    for block in blocks:
        font, cfg = _font_for_block(block, canvas_min_edge)
        text = block.get("text", "") or ""
        color = _parse_color(block.get("color"), default=(20, 20, 20, 255))
        marker = cfg.get("marker", "")
        prefix_w = draw.textbbox((0, 0), marker, font=font)[2] if marker else 0
        lines = _wrap(draw, text, font, max(max_width - prefix_w, 1))
        line_h = int(font.size * cfg["line_height"])
        for i, line in enumerate(lines):
            line_w = draw.textbbox((0, 0), line, font=font)[2]
            if align == "center":
                lx = x + (max_width - (line_w + prefix_w)) // 2
            elif align == "right":
                lx = x + max_width - line_w - prefix_w
            else:
                lx = x
            if marker and i == 0:
                draw.text((lx, cursor_y), marker, font=font, fill=color)
            draw.text((lx + prefix_w, cursor_y), line, font=font, fill=color)
            cursor_y += line_h
        cursor_y += int(font.size * cfg["after"])
    return canvas


def _parse_color(c: Optional[str], default=(0, 0, 0, 255)) -> tuple[int, int, int, int]:
    if not c:
        return default
    c = c.strip()
    if c.startswith("#"):
        h = c.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
    named = {
        "white": (255, 255, 255, 255),
        "black": (20, 20, 20, 255),
        "coral": (255, 99, 88, 255),
        "navy":  (28, 47, 92, 255),
    }
    return named.get(c.lower(), default)


def composite_typography_block(
    base_bytes: bytes,
    blocks: list[dict],
    *,
    bbox: dict,
    align: str = "left",
    background_color: Optional[str] = None,
) -> bytes:
    """Render text blocks into a region of the base image. Returns PNG bytes.

    bbox: {x, y, w, h} in [0,1] — fraction of base image.
    background_color: optional rgba hex/name to draw under the text region.
    """
    base = Image.open(BytesIO(base_bytes)).convert("RGBA")
    img_w, img_h = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    rx = int(bbox["x"] * img_w)
    ry = int(bbox["y"] * img_h)
    rw = int(bbox["w"] * img_w)
    rh = int(bbox["h"] * img_h)
    pad = int(min(img_w, img_h) * 0.03)

    if background_color:
        bg = _parse_color(background_color, default=(255, 255, 255, 235))
        draw.rectangle((rx, ry, rx + rw, ry + rh), fill=bg)

    render_text_blocks(
        overlay, blocks,
        x=rx + pad, y=ry + pad,
        max_width=max(rw - 2 * pad, 1),
        align=align,
    )
    out = Image.alpha_composite(base, overlay)
    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
