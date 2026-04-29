"""Deterministic text overlays via PIL (zero API cost, exact characters).

The reason this module exists: gpt-image-2 is materially better than nanobanana at
text, but still not perfect. For URL/CTA copy where `proven.com/spring` MUST NOT
become `proven.com/sprlng`, we render the text deterministically and composite over
the source image. Output is always PNG.

Public surface:
  - render_text_overlay(src_bytes, *, text, position, bbox, style, color) -> bytes
  - render_link_cta(src_bytes, *, cta_text, url_text, position, bbox, style, color_scheme) -> bytes

Position is one of POSITION_PRESETS, OR pass an explicit bbox={x,y,w,h} in [0,1].
"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# ---------------------------------------------------------------------------
# Position presets — fractions of (width, height). Pad applied separately.
# ---------------------------------------------------------------------------
POSITION_PRESETS = {
    "top":            ("center", "top"),
    "top-left":       ("left",   "top"),
    "top-right":      ("right",  "top"),
    "bottom":         ("center", "bottom"),
    "bottom-left":    ("left",   "bottom"),
    "bottom-right":   ("right",  "bottom"),
    "bottom-center":  ("center", "bottom"),
    "center":         ("center", "center"),
    "left":           ("left",   "center"),
    "right":          ("right",  "center"),
}


# ---------------------------------------------------------------------------
# Font discovery — bundled Inter > IBM Plex Sans > system DejaVu/Helvetica > PIL default.
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = {
    "regular": [
        os.path.join(FONTS_DIR, "Inter-Regular.ttf"),
        os.path.join(FONTS_DIR, "IBMPlexSans-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "Arial.ttf",
    ],
    "bold": [
        os.path.join(FONTS_DIR, "Inter-Bold.ttf"),
        os.path.join(FONTS_DIR, "IBMPlexSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "Arial Bold.ttf",
    ],
    "mono": [
        os.path.join(FONTS_DIR, "JetBrainsMono-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "Courier New.ttf",
    ],
}


def _load_font(weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES.get(weight, _FONT_CANDIDATES["regular"]):
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Style configuration — tuned for legibility on photographic backgrounds.
# ---------------------------------------------------------------------------
TEXT_STYLES = {
    "headline": {
        "weight": "bold",
        "size_frac": 0.07,         # 7% of min(w,h)
        "padding": 0.025,
        "shadow": True,
        "uppercase": False,
    },
    "badge": {
        "weight": "bold",
        "size_frac": 0.045,
        "padding": 0.02,
        "shadow": False,
        "background": True,        # rounded rect behind text
        "uppercase": True,
    },
    "caption": {
        "weight": "regular",
        "size_frac": 0.03,
        "padding": 0.02,
        "shadow": True,
        "uppercase": False,
    },
    "watermark": {
        "weight": "regular",
        "size_frac": 0.022,
        "padding": 0.015,
        "shadow": False,
        "opacity": 180,
        "uppercase": False,
    },
}

CTA_STYLES = {
    "button":          {"weight": "bold",    "size_frac": 0.035, "padding": 0.020, "rounded": True,  "bar": False, "url_mono": True},
    "bar":             {"weight": "regular", "size_frac": 0.030, "padding": 0.018, "rounded": False, "bar": True,  "url_mono": True},
    "pill":            {"weight": "bold",    "size_frac": 0.030, "padding": 0.022, "rounded": True,  "bar": False, "url_mono": True, "pill": True},
    "underlined-link": {"weight": "regular", "size_frac": 0.030, "padding": 0.015, "rounded": False, "bar": False, "url_mono": True, "underline": True},
}

COLOR_SCHEMES = {
    # name -> (bg_rgba, fg_rgba)
    "white-on-coral":   ((255, 99, 88, 235),    (255, 255, 255, 255)),
    "coral-on-white":   ((255, 255, 255, 235),  (255, 99, 88, 255)),
    "white-on-black":   ((20, 20, 20, 220),     (255, 255, 255, 255)),
    "black-on-white":   ((255, 255, 255, 235),  (20, 20, 20, 255)),
    "white-on-navy":    ((28, 47, 92, 235),     (255, 255, 255, 255)),
    "white-on-forest":  ((34, 80, 50, 235),     (255, 255, 255, 255)),
}


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------
def _parse_color(c: Optional[str], default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    if not c:
        return default
    c = c.strip()
    if c.startswith("#"):
        h = c.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return (r, g, b, 255)
        if len(h) == 8:
            r, g, b, a = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
            return (r, g, b, a)
    named = {
        "white": (255, 255, 255, 255),
        "black": (0, 0, 0, 255),
        "coral": (255, 99, 88, 255),
        "navy":  (28, 47, 92, 255),
        "red":   (220, 38, 38, 255),
    }
    return named.get(c.lower(), default)


# ---------------------------------------------------------------------------
# Geometry — preset / bbox -> pixel rectangle for the text block
# ---------------------------------------------------------------------------
def _resolve_box(
    img_w: int, img_h: int,
    *, position: Optional[str], bbox: Optional[dict], padding_frac: float
) -> tuple[int, int, int, int]:
    """Return (anchor_x, anchor_y, max_w, max_h) for the text block."""
    pad = int(min(img_w, img_h) * padding_frac)

    if bbox is not None:
        x = int(bbox.get("x", 0) * img_w)
        y = int(bbox.get("y", 0) * img_h)
        w = int(bbox.get("w", 1) * img_w)
        h = int(bbox.get("h", 1) * img_h)
        return x, y, max(w, 1), max(h, 1)

    if position is None:
        position = "bottom-center"
    if position not in POSITION_PRESETS:
        raise ValueError(
            f"Unknown position '{position}'. Use one of {sorted(POSITION_PRESETS)} "
            "or pass an explicit bbox={x,y,w,h} in [0,1]."
        )
    halign, valign = POSITION_PRESETS[position]
    block_w = img_w - 2 * pad
    block_h = int(img_h * 0.25)  # reserve ~25% for the text region by default
    if halign == "left":
        x = pad
    elif halign == "right":
        x = img_w - pad - block_w
    else:
        x = (img_w - block_w) // 2
    if valign == "top":
        y = pad
    elif valign == "bottom":
        y = img_h - pad - block_h
    else:
        y = (img_h - block_h) // 2
    return x, y, block_w, block_h


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


# ---------------------------------------------------------------------------
# Public: render_text_overlay
# ---------------------------------------------------------------------------
def render_text_overlay(
    src_bytes: bytes,
    *,
    text: str,
    position: Optional[str] = None,
    bbox: Optional[dict] = None,
    style: str = "headline",
    color: Optional[str] = None,
) -> bytes:
    """Composite `text` over the source image at the given position/bbox. Always returns PNG."""
    if not text or not isinstance(text, str):
        raise ValueError("text must be a non-empty string.")
    if style not in TEXT_STYLES:
        raise ValueError(
            f"Unknown style '{style}'. One of: {sorted(TEXT_STYLES)}."
        )

    base = Image.open(BytesIO(src_bytes)).convert("RGBA")
    img_w, img_h = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    cfg = TEXT_STYLES[style]
    font_size = max(12, int(min(img_w, img_h) * cfg["size_frac"]))
    font = _load_font(cfg["weight"], font_size)

    display_text = text.upper() if cfg.get("uppercase") else text
    fg = _parse_color(color, default=(255, 255, 255, 255))
    if "opacity" in cfg:
        fg = (fg[0], fg[1], fg[2], cfg["opacity"])

    box_x, box_y, box_w, _ = _resolve_box(img_w, img_h, position=position, bbox=bbox, padding_frac=cfg["padding"])

    lines = _wrap_text(draw, display_text, font, box_w)
    line_h = _text_size(draw, "Ag", font)[1]
    total_h = line_h * len(lines) + int(line_h * 0.2 * (len(lines) - 1))

    y_cursor = box_y + max(0, _resolve_y_within(img_h, position, bbox, box_y, total_h))

    if cfg.get("background"):
        max_line_w = max(_text_size(draw, ln, font)[0] for ln in lines)
        bg_pad = int(font_size * 0.6)
        bx0 = box_x + (box_w - max_line_w) // 2 - bg_pad
        by0 = y_cursor - bg_pad // 2
        bx1 = bx0 + max_line_w + 2 * bg_pad
        by1 = y_cursor + total_h + bg_pad // 2
        bg_color = (*_parse_color(color, default=(255, 99, 88, 235))[:3], 235)
        draw.rounded_rectangle((bx0, by0, bx1, by1), radius=bg_pad, fill=bg_color)
        fg = (255, 255, 255, 255)

    for line in lines:
        line_w, _ = _text_size(draw, line, font)
        x = box_x + (box_w - line_w) // 2
        if cfg.get("shadow"):
            offset = max(2, font_size // 24)
            draw.text((x + offset, y_cursor + offset), line, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y_cursor), line, font=font, fill=fg)
        y_cursor += line_h + int(line_h * 0.2)

    out = Image.alpha_composite(base, overlay)
    buf = BytesIO()
    out.convert("RGB" if out.mode == "RGB" else "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _resolve_y_within(
    img_h: int, position: Optional[str], bbox: Optional[dict],
    box_y: int, content_h: int,
) -> int:
    """Vertically center content within the resolved box for top/center/bottom presets."""
    if bbox is not None:
        return 0
    if position in (None, "bottom", "bottom-center", "bottom-left", "bottom-right"):
        return max(0, (img_h - box_y) - content_h - int(img_h * 0.025))
    if position in ("top", "top-left", "top-right"):
        return 0
    return 0  # center handled by initial box position


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    if max_width <= 0:
        return [text]
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = current + " " + w
        if _text_size(draw, candidate, font)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Public: render_link_cta
# ---------------------------------------------------------------------------
def render_link_cta(
    src_bytes: bytes,
    *,
    cta_text: str,
    url_text: str,
    position: Optional[str] = None,
    bbox: Optional[dict] = None,
    style: str = "button",
    color_scheme: str = "white-on-coral",
) -> bytes:
    """Render a CTA + URL composite. URL text is exact (no AI generation)."""
    if not url_text:
        raise ValueError("url_text is required and must match the intended URL exactly.")
    if style not in CTA_STYLES:
        raise ValueError(f"Unknown CTA style '{style}'. One of: {sorted(CTA_STYLES)}.")
    if color_scheme not in COLOR_SCHEMES:
        raise ValueError(f"Unknown color_scheme '{color_scheme}'. One of: {sorted(COLOR_SCHEMES)}.")

    base = Image.open(BytesIO(src_bytes)).convert("RGBA")
    img_w, img_h = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    cfg = CTA_STYLES[style]
    bg_color, fg_color = COLOR_SCHEMES[color_scheme]
    cta_size = max(14, int(min(img_w, img_h) * cfg["size_frac"]))
    url_size = max(11, int(cta_size * 0.8))
    cta_font = _load_font(cfg["weight"], cta_size)
    url_font = _load_font("mono" if cfg.get("url_mono") else "regular", url_size)

    cta_w, cta_h = _text_size(draw, cta_text or "", cta_font) if cta_text else (0, 0)
    url_w, url_h = _text_size(draw, url_text, url_font)

    inner_pad = int(cta_size * 0.6)
    gap = int(cta_size * 0.3) if cta_text else 0

    block_w = max(cta_w, url_w) + 2 * inner_pad
    block_h = (cta_h + gap + url_h) + 2 * inner_pad if cta_text else url_h + 2 * inner_pad

    box_x, box_y, box_max_w, _ = _resolve_box(img_w, img_h, position=position, bbox=bbox, padding_frac=cfg["padding"])
    block_w = min(block_w, box_max_w)

    if position in (None, "bottom", "bottom-center", "bottom-left", "bottom-right") and bbox is None:
        anchor_x = box_x + (box_max_w - block_w) // 2
        anchor_y = img_h - block_h - int(min(img_w, img_h) * cfg["padding"])
    else:
        anchor_x = box_x + (box_max_w - block_w) // 2
        anchor_y = box_y

    if cfg.get("bar"):
        bar_y0 = img_h - block_h - 2
        draw.rectangle((0, bar_y0, img_w, img_h), fill=bg_color)
        anchor_x = (img_w - max(cta_w, url_w)) // 2
        anchor_y = bar_y0 + inner_pad
    elif cfg.get("rounded"):
        radius = int(block_h / 2) if cfg.get("pill") else int(cta_size * 0.4)
        draw.rounded_rectangle(
            (anchor_x, anchor_y, anchor_x + block_w, anchor_y + block_h),
            radius=radius, fill=bg_color,
        )

    text_y = anchor_y + inner_pad
    if cta_text:
        cx = anchor_x + (block_w - cta_w) // 2
        draw.text((cx, text_y), cta_text, font=cta_font, fill=fg_color)
        text_y += cta_h + gap
    ux = anchor_x + (block_w - url_w) // 2
    draw.text((ux, text_y), url_text, font=url_font, fill=fg_color)
    if cfg.get("underline"):
        draw.line(
            (ux, text_y + url_h + 2, ux + url_w, text_y + url_h + 2),
            fill=fg_color, width=max(1, url_size // 14),
        )

    out = Image.alpha_composite(base, overlay)
    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
