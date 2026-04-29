"""Versioned prompt templates.

v0.1 only needs the AI-mode fallback templates for `add_text_overlay` and `add_link_cta`.
The deterministic path doesn't use these; they're invoked only if a user passes
render_mode="ai_integrated" (a v0.2/v0.3 surface — present in v0.1 as a thin wrapper
around edit_image so the API surface is stable).
"""

from __future__ import annotations


PROMPT_VERSION = "v0.1"


def text_overlay_ai(text: str, *, position: str, style: str, color: str | None) -> str:
    """AI-integrated fallback for add_text_overlay (used if render_mode='ai_integrated')."""
    color_phrase = f" in {color}" if color else ""
    return (
        f"Add the text \"{text}\" as a {style} overlay on this image, "
        f"positioned at {position}{color_phrase}. "
        "Render the text exactly as written, character-for-character, no paraphrasing. "
        "Match the existing image style; do not alter the rest of the image."
    )


def link_cta_ai(cta_text: str, url_text: str, *, position: str, style: str, color_scheme: str) -> str:
    """AI-integrated fallback for add_link_cta."""
    return (
        f"Add a {style} call-to-action with the text \"{cta_text}\" "
        f"and URL \"{url_text}\" to this image at {position}, "
        f"styled in {color_scheme}. "
        "Render both strings EXACTLY as written, character-for-character. "
        "URL accuracy is critical — every character must match."
    )
