"""@mcp.tool definitions — thin wrappers over the adapters.

Image-output tools return a 3-part FastMCP block list:

  [Image(thumb_jpeg), ..., render_md_str, json_str]

  - `Image(...)` blocks: 1024px JPEG thumbnails (one per generated image) — the
    MCP host renders these inline in the tool pane and the chat reply.
  - `render_md_str`: markdown image embeds + download links + a Save-to-Drive
    nudge. Agents must surface this verbatim so users get reliable links.
  - `json_str`: full metadata dict as JSON, last so `_api_json` can `[-1]` it.

The JSON payload contains:

  {
    "asset_id": "img_...",
    "image_url": "...durable or signed...",
    "image_url_signed": "...",        # only when cloud storage is configured
    "expires_at": "ISO-8601 UTC" | null,
    "refresh_url_supported": true,
    "width": int, "height": int, "size_kb": int, "mime": "image/png",
    "usage": {...}, "estimated_cost_usd": float, "actual_cost_usd": float|None,
    "attempt_count": int, "validation_status": "ok" | ...,
  }
For multi-image responses (count>1), `images: [...]` carries one entry per result.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from typing import Annotated, Optional

from mcp.server.fastmcp import Image
from pydantic import Field

from costs import attach_cost_meta
from deterministic_render import render_link_cta, render_text_overlay
from image_io import (
    fetch_url,
    get_image_dimensions,
    is_url,
    normalize_image,
    resolve_image_input,
    validate_mask,
)
from infographic import (
    LAYOUTS,
    composite_infographic_text,
    generate_infographic_background,
    list_layouts,
)
from openai_images import call_edit, call_generate, size_for_aspect
from openai_responses import call_refine
from prompts import link_cta_ai, text_overlay_ai
from storage import (
    cloud_storage_configured,
    make_signed_url,
    store_asset,
    upload_to_cloud,
)


SAVE_TO_DRIVE_NUDGE = (
    "**Save to Google Drive?** Reply *save* and I'll upload via the Google Drive MCP."
)


def _public_base_url() -> str:
    """Best-effort base URL for snippet generation.

    Used in upload-error messages so the agent points at the right host. Cloud
    Run sets `PUBLIC_URL`; local dev falls back to host:port.
    """
    public = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if public:
        return public
    host = os.environ.get("HOST", "localhost")
    if host == "0.0.0.0":
        host = "localhost"
    port = os.environ.get("PORT", "8080")
    return f"http://{host}:{port}"


def _bearer_header_line() -> str:
    """If MCP_BEARER_TOKEN is set, surface the auth header as a separate line."""
    if os.environ.get("MCP_BEARER_TOKEN"):
        return "    req.add_header('Authorization', f'Bearer {os.environ[\"MCP_BEARER_TOKEN\"]}')\n"
    return ""


def _urllib_snippet() -> str:
    """Upload snippet for **claude.ai web AND Claude Cowork** (Python tool).

    Auto-discovers the uploads folder by trying known paths in order:
      1. /mnt/user-data/uploads — claude.ai web sandbox
      2. ~/Library/Application Support/Claude/local-agent-mode-sessions/<session>/uploads
         — Claude Cowork (Mac); picks the most recently modified session.

    Returned response includes `asset_id` (durable opaque ref — pass it to any
    tool) plus `image_url` (HMAC-signed, 24h TTL).
    """
    server_url = _public_base_url()
    auth_line = _bearer_header_line()
    return (
        "import urllib.request, json, os, time, glob\n"
        f"SERVER = '{server_url}'\n"
        "candidates = ['/mnt/user-data/uploads'] + sorted(\n"
        "    glob.glob(os.path.expanduser('~/Library/Application Support/Claude/local-agent-mode-sessions/*/uploads')),\n"
        "    key=os.path.getmtime, reverse=True)\n"
        "uploads = next((p for p in candidates if os.path.isdir(p)), None)\n"
        "if not uploads:\n"
        "    print(f'No uploads folder found. Upload manually at {SERVER}/upload')\n"
        "else:\n"
        "    files = sorted([f for f in os.listdir(uploads) if f.lower().endswith(('.png','.jpg','.jpeg','.webp'))],\n"
        "                   key=lambda f: os.path.getmtime(os.path.join(uploads, f)), reverse=True)[:4]\n"
        "    for i, fname in enumerate(files):\n"
        "        with open(os.path.join(uploads, fname), 'rb') as fh: data = fh.read()\n"
        "        req = urllib.request.Request(f'{SERVER}/upload', data=data, method='POST')\n"
        + auth_line +
        "        for attempt in range(3):\n"
        "            try:\n"
        "                result = json.loads(urllib.request.urlopen(req, timeout=30).read())\n"
        "                print(f'image{i}: asset_id={result[\"asset_id\"]}  image_url={result[\"image_url\"]}')\n"
        "                break\n"
        "            except Exception as e:\n"
        "                if attempt < 2: time.sleep(2 ** attempt)\n"
        "                else: print(f'image{i}: FAILED: {e}. Upload manually at {SERVER}/upload')\n"
    )


def _claude_code_snippet(path: Optional[str] = None) -> str:
    """Upload snippet for **Claude Code** (Bash tool, real local filesystem).

    Claude Code has shell access and can read the user's actual files, so curl
    with --data-binary is the simplest path. When `path` is provided, it's
    substituted into the command so the agent runs it verbatim.
    """
    server_url = _public_base_url()
    target = path or "/full/path/to/image.jpg"
    auth_flag = ""
    if os.environ.get("MCP_BEARER_TOKEN"):
        auth_flag = ' -H "Authorization: Bearer $MCP_BEARER_TOKEN"'
    return (
        f'curl -fsS -X POST{auth_flag} --data-binary "@{target}" \\\n'
        f'  "{server_url}/upload" \\\n'
        '  | python3 -c "import sys,json; d=json.load(sys.stdin); print(\'asset_id=\'+d.get(\'asset_id\',\'\')+\'  image_url=\'+d.get(\'image_url\',\'\'))"'
    )


def _upload_help_message(reason: str, candidate_path: Optional[str] = None) -> str:
    """Compose a multi-environment fix-it message for upload_image errors."""
    base = _public_base_url()
    return (
        f"{reason}\n\n"
        "Pick the option for your environment:\n\n"
        "**A. claude.ai web OR Claude Cowork — run with the Python tool (NOT bash):**\n\n"
        f"```python\n{_urllib_snippet()}```\n\n"
        "**B. Claude Code (CLI) — run with the Bash tool:**\n\n"
        f"```bash\n{_claude_code_snippet(candidate_path)}\n```\n\n"
        f"**C. Manual upload:** {base}/upload   •   **Studio UI:** {base}/app"
    )


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------
def _make_thumbnail(img_bytes: bytes, max_dim: int = 1024) -> bytes:
    """Build a small JPEG for inline rendering in the tool pane / chat reply.

    Native MCP `Image` blocks must fit cleanly into agent output — we cap the
    long edge at 1024 and re-encode as quality-85 JPEG.
    """
    from PIL import Image as PILImage
    pil = PILImage.open(BytesIO(img_bytes))
    if max(pil.size) > max_dim:
        pil.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
    if pil.mode != "RGB":
        if pil.mode == "RGBA":
            bg = PILImage.new("RGB", pil.size, (255, 255, 255))
            bg.paste(pil, mask=pil.split()[-1])
            pil = bg
        else:
            pil = pil.convert("RGB")
    buf = BytesIO()
    pil.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _render_markdown(items: list[dict], save_nudge: bool = True) -> str:
    """Build the markdown image embed + download link block.

    `items` is the list of per-image metadata dicts (each with `image_url`).
    The tail nudges the user toward Save-to-Drive — if they reply *save* the
    agent fetches the bytes from `image_url` and calls Google Drive MCP.
    """
    if not items:
        return ""
    if len(items) == 1:
        url = items[0].get("image_url", "")
        body = f"![]({url})\n\n[Download image]({url})"
    else:
        parts = []
        for i, item in enumerate(items, 1):
            url = item.get("image_url", "")
            if not url:
                continue
            parts.append(f"![Image {i}]({url})\n[Download image {i}]({url})")
        body = "\n\n".join(parts)
    if save_nudge:
        body = body + f"\n\n{SAVE_TO_DRIVE_NUDGE}"
    return body


async def _build_response(
    image_bytes_list: list[bytes],
    *,
    mime: str = "image/png",
    cost_meta: dict,
    save_folder: Optional[str] = None,
    prefix: str = "img",
    save_nudge: bool = True,
    extras: Optional[dict] = None,
) -> list:
    """Store each image, build native FastMCP blocks for inline rendering.

    Returns `[Image(thumb_jpeg), ..., render_md_str, json_str]` where:
      - `Image(...)` blocks render in the tool pane / chat reply.
      - `render_md_str` carries markdown image embeds + download links.
      - `json_str` carries the full metadata dict for tool chaining and the
        `/api/{tool}` shim.

    If S3_BUCKET or GCS_BUCKET is set, also upload to cloud and surface that
    URL as `image_url` (durable, no expiry). The signed in-memory URL is kept
    as `image_url_signed` for parity / debugging.
    """
    items = []
    thumbs: list[bytes] = []
    use_cloud = cloud_storage_configured()
    for i, b in enumerate(image_bytes_list):
        try:
            w, h = get_image_dimensions(b)
        except Exception:
            w, h = 0, 0
        asset_id = store_asset(b, mime)
        signed_url, expires_at = make_signed_url(asset_id)
        durable_url: Optional[str] = None
        if use_cloud:
            durable_url = await upload_to_cloud(b, mime, asset_id=asset_id)
        item = {
            "asset_id": asset_id,
            "image_url": durable_url or signed_url,
            "expires_at": None if durable_url else expires_at.isoformat(),
            "refresh_url_supported": True,
            "width": w, "height": h,
            "size_kb": len(b) // 1024,
            "mime": mime,
        }
        if durable_url:
            item["image_url_signed"] = signed_url
        if save_folder:
            saved = _save_local(b, save_folder, prefix=prefix, idx=i, ext=_ext_for_mime(mime))
            if saved:
                item["saved_to"] = saved
        items.append(item)
        try:
            thumbs.append(_make_thumbnail(b))
        except Exception:
            pass

    if len(items) == 1:
        payload = dict(items[0])
    else:
        payload = {"images": items}
    payload.update(cost_meta)
    if extras:
        payload.update(extras)

    render_md = _render_markdown(items, save_nudge=save_nudge)
    json_str = json.dumps(payload, default=str)
    blocks: list = [Image(data=t, format="jpeg") for t in thumbs]
    blocks.append(render_md)
    blocks.append(json_str)
    return blocks


def unpack_tool_result(result) -> dict:
    """Pull the JSON metadata dict out of a tool's block list.

    Image-output tools return `[Image, ..., render_md, json_str]`. Most callers
    (tests, the /api/{tool} shim, agent chaining) want the dict at the end.
    Falls back to identity if the tool already returned a dict (legacy paths).
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        last = result[-1]
        if isinstance(last, str):
            try:
                return json.loads(last)
            except json.JSONDecodeError:
                pass
        if isinstance(last, dict):
            return last
    raise TypeError(f"Cannot extract dict from tool result of type {type(result).__name__}")


def _ext_for_mime(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")


def _save_local(b: bytes, folder: str, *, prefix: str, idx: int, ext: str) -> Optional[str]:
    try:
        os.makedirs(folder, exist_ok=True)
        import time
        path = os.path.join(folder, f"{prefix}_{int(time.time())}_{idx}.{ext}")
        with open(path, "wb") as f:
            f.write(b)
        return path
    except Exception:
        return None


def _output_format_to_mime(fmt: str) -> str:
    return {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
def register_tools(mcp) -> None:

    @mcp.tool(
        description=(
            "Upload an image to the gpt-image-2-mcp server. Accepts an http(s) URL or "
            "(in stdio mode) a local file path. Returns an asset_id and signed image_url "
            "that any other tool will accept as input. NEVER pass base64 / data: URIs — "
            "they hang MCP transport. For pasted/local files in claude.ai web, POST to "
            "/upload to get an asset_id."
        )
    )
    async def upload_image(
        image: Annotated[str, Field(description="http(s) URL, or local file path (stdio only)")],
    ) -> list:
        image = image.strip()
        if (image.startswith('"') and image.endswith('"')) or (
            image.startswith("'") and image.endswith("'")
        ):
            image = image[1:-1]

        candidate_path = image if (not is_url(image) and not image.startswith("data:")) else None

        if image.startswith("data:") or _looks_like_b64(image):
            raise ValueError(
                _upload_help_message(
                    "Base64 / data: URIs are NOT accepted as MCP parameters — they hang "
                    "transport regardless of size. Do NOT re-encode at lower quality and retry.",
                    candidate_path=candidate_path,
                )
            )

        if is_url(image):
            raw, _mime = fetch_url(image)
        elif os.path.isfile(image):
            with open(image, "rb") as f:
                raw = f.read()
        else:
            raise ValueError(
                _upload_help_message(
                    "image must be an http(s) URL or a local file path (stdio only). "
                    "The path you provided does not exist on this server (this is normal "
                    "when the MCP runs remotely — the server can't see your local filesystem).",
                    candidate_path=candidate_path,
                )
            )

        normalized, mime = normalize_image(raw, max_dim=2048, target_format="PNG")
        cost_meta = attach_cost_meta(
            usage={}, quality="n/a", n=1, has_image_input=False, prompt_chars=0,
            attempt_count=1, validation_status="ok",
        )
        cost_meta["estimated_cost_usd"] = 0.0
        cost_meta["actual_cost_usd"] = 0.0
        return await _build_response(
            [normalized], mime=mime, cost_meta=cost_meta, prefix="upload",
            save_nudge=False,
        )

    @mcp.tool(
        description=(
            "Text-to-image with gpt-image-2 — strong on text, infographics, "
            "multi-reference compositing, identity-preserving edits. "
            "For artistic / photoreal raw assets prefer Nano Banana. "
            "n>1 returns variants (NOT a coherent set). PNG default. "
            "INTAKE REQUIRED: confirm aspect_ratio AND quality with the user "
            "before calling, unless they already named both."
        )
    )
    async def generate_image(
        prompt: Annotated[str, Field(min_length=1, max_length=4000)],
        aspect_ratio: Annotated[str, Field(description="Confirm with user. 1:1, 4:5, 5:4, 16:9, 9:16, 3:2, 2:3, social-*, infographic-*.")] = "1:1",
        quality: Annotated[str, Field(description="Confirm with user. low | medium (default) | high (4K, costs more).")] = "medium",
        count: Annotated[int, Field(ge=1, le=4)] = 1,
        output_format: Annotated[str, Field(description="png | jpeg | webp")] = "png",
        moderation: Annotated[Optional[str], Field(description="auto | low (rarely needed)")] = None,
        save_folder: Annotated[Optional[str], Field(description="Local folder to also save into")] = None,
    ) -> list:
        size = size_for_aspect(aspect_ratio, prefer_high=(quality == "high"))
        images, usage = await call_generate(
            prompt=prompt,
            size=size,
            n=count,
            quality=quality,
            output_format=output_format,
            moderation=moderation,
        )
        mime = _output_format_to_mime(output_format)
        cost_meta = attach_cost_meta(
            usage=usage, quality=quality, n=count,
            has_image_input=False, prompt_chars=len(prompt), attempt_count=1,
        )
        return await _build_response(images, mime=mime, cost_meta=cost_meta,
                               save_folder=save_folder, prefix="generated")

    @mcp.tool(
        description=(
            "Edit an image with gpt-image-2. Up to 16 source images, optional mask. "
            "Sources may be http(s) URLs OR asset_ids. "
            "Output shape DEFAULTS to the first source's closest aspect alias — "
            "no intake needed unless the user explicitly asks to reshape. "
            "Masks guide where edits should occur — they don't enforce pixel-precise "
            "selection. Mask must be PNG, same size as base, with alpha "
            "(black/white-only PNGs are auto-converted)."
        )
    )
    async def edit_image(
        prompt: Annotated[str, Field(min_length=1, max_length=4000)],
        image: Annotated[str | list[str], Field(description="URL or asset_id, or list of up to 16")],
        mask: Annotated[Optional[str], Field(description="URL or asset_id of a mask PNG, optional")] = None,
        aspect_ratio: Annotated[Optional[str], Field(
            description="Optional. Defaults to the closest alias for the first source image's shape — pass to override.")] = None,
        quality: Annotated[str, Field()] = "medium",
        count: Annotated[int, Field(ge=1, le=4)] = 1,
        output_format: Annotated[str, Field()] = "png",
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        refs: list[str] = [image] if isinstance(image, str) else list(image)
        if not refs:
            raise ValueError("At least one source image is required.")
        if len(refs) > 16:
            raise ValueError(f"At most 16 source images allowed; got {len(refs)}.")

        sources: list[tuple[bytes, str]] = []
        for ref in refs:
            b, m = resolve_image_input(ref)
            sources.append((b, m))

        if aspect_ratio:
            size = size_for_aspect(aspect_ratio, prefer_high=(quality == "high"))
        else:
            # Default to source-image shape — matches the intake-skip convention.
            base_w, base_h = get_image_dimensions(sources[0][0])
            size = _closest_aspect_size(base_w, base_h, prefer_high=(quality == "high"))

        mask_bytes: Optional[bytes] = None
        if mask:
            mb, _ = resolve_image_input(mask)
            base_w, base_h = get_image_dimensions(sources[0][0])
            mask_bytes = validate_mask(mb, base_w, base_h)

        images, usage = await call_edit(
            prompt=prompt,
            images=sources,
            mask=mask_bytes,
            size=size,
            n=count,
            quality=quality,
            output_format=output_format,
        )
        mime = _output_format_to_mime(output_format)
        cost_meta = attach_cost_meta(
            usage=usage, quality=quality, n=count,
            has_image_input=True, prompt_chars=len(prompt), attempt_count=1,
        )
        return await _build_response(images, mime=mime, cost_meta=cost_meta,
                               save_folder=save_folder, prefix="edited")

    @mcp.tool(
        description=(
            "Iteratively refine an image via the Responses API (multi-turn). "
            "Use for follow-up edits like 'make the background darker', 'tighten the "
            "left margin', 'shift the subject up'. Chain across turns by passing "
            "previous_response_id from the prior call — preserves model context "
            "without re-uploading. tool_choice is always forced to image_generation; "
            "you cannot accidentally get a text-only reply. "
            "First turn: pass image=URL/asset_id. Subsequent turns: pass previous_response_id."
        )
    )
    async def refine_image(
        prompt: Annotated[str, Field(min_length=1, max_length=4000,
            description="What to change. Be specific — 'darken background by 20%', not 'make it cooler'.")],
        image: Annotated[Optional[str], Field(
            description="URL or asset_id of the source image. Required on first turn; ignored if previous_response_id is set.")] = None,
        previous_response_id: Annotated[Optional[str], Field(
            description="response_id from the prior refine_image call (multi-turn chaining).")] = None,
        image_generation_call_id: Annotated[Optional[str], Field(
            description="image_generation_call.id from a prior turn — alternative chaining mode.")] = None,
        reasoning_effort: Annotated[Optional[str], Field(
            description="low|medium|high — defaults to OPENAI_REFINE_REASONING_EFFORT (low).")] = None,
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        if previous_response_id and image_generation_call_id:
            raise ValueError("Pass at most one of previous_response_id or image_generation_call_id.")
        if not (image or previous_response_id or image_generation_call_id):
            raise ValueError(
                "First turn requires image=URL/asset_id; subsequent turns require "
                "previous_response_id or image_generation_call_id."
            )

        image_bytes: Optional[bytes] = None
        image_mime: str = "image/png"
        if image and not (previous_response_id or image_generation_call_id):
            image_bytes, image_mime = resolve_image_input(image)

        images, usage, response_id = await call_refine(
            prompt=prompt,
            image_bytes=image_bytes,
            image_mime=image_mime,
            previous_response_id=previous_response_id,
            image_generation_call_id=image_generation_call_id,
            reasoning_effort=reasoning_effort,
        )

        cost_meta = attach_cost_meta(
            usage=usage, quality="medium", n=1,
            has_image_input=bool(image_bytes), prompt_chars=len(prompt), attempt_count=1,
        )
        return await _build_response(
            images, mime="image/png", cost_meta=cost_meta,
            save_folder=save_folder, prefix="refined",
            extras={"response_id": response_id},
        )

    @mcp.tool(
        description=(
            "Generate an infographic with EXACT text. Hybrid pipeline: "
            "(1) gpt-image-2 generates the BACKGROUND only — no text in the image; "
            "(2) the title / subtitle / stat / bullets / footer you provide are "
            "rendered deterministically via Pillow on top of the background. "
            "URL accuracy and stat accuracy are guaranteed — the model never sees them. "
            "Layouts: portrait-stat (big stat + label), landscape-comparison "
            "(title + two columns), square-list (title + bullets). "
            "Slot names depend on the layout — see content schema in the docs. "
            "background_prompt should describe the visual style of the background only. "
            "INTAKE REQUIRED: confirm layout AND quality with the user before "
            "calling. Layout implies aspect (portrait-stat=2:3, landscape-comparison=3:2, "
            "square-list=1:1) but quality (low/medium/high) is the user's call."
        )
    )
    async def create_infographic(
        background_prompt: Annotated[str, Field(min_length=1, max_length=2000,
            description="Visual style for the background — do not include text content here.")],
        layout: Annotated[str, Field(
            description="portrait-stat | landscape-comparison | square-list")] = "square-list",
        content: Annotated[dict, Field(description=(
            "Slot text. Keys depend on layout: "
            "portrait-stat → {stat, subtitle, footer}; "
            "landscape-comparison → {title, left (str|list), right (str|list)}; "
            "square-list → {title, subtitle, bullets (list[str]), footer}."
        ))] = ...,
        quality: Annotated[str, Field()] = "medium",
        color: Annotated[Optional[str], Field(description="Text color override (hex or named).")] = None,
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        if layout not in LAYOUTS:
            raise ValueError(f"Unknown layout '{layout}'. One of: {list_layouts()}.")
        if not isinstance(content, dict) or not content:
            raise ValueError("content must be a non-empty dict of slot text.")

        bg_bytes, usage = await generate_infographic_background(
            prompt=background_prompt, layout=layout, quality=quality,
        )
        composed = composite_infographic_text(
            bg_bytes, layout=layout, content=content, color=color,
        )
        cost_meta = attach_cost_meta(
            usage=usage, quality=quality, n=1,
            has_image_input=False, prompt_chars=len(background_prompt), attempt_count=1,
        )
        return await _build_response([composed], mime="image/png", cost_meta=cost_meta,
                                     save_folder=save_folder, prefix="infographic")

    @mcp.tool(
        description=(
            "Combine 2–16 source images into one new image with gpt-image-2. "
            "Use for: product-on-background, side-by-side layouts, character-into-scene, "
            "multi-reference identity-preserving compositions. "
            "Sources may be URLs or asset_ids in any mix. "
            "Distinct from edit_image: framed as 'create a new image from these inputs', "
            "not 'modify this image'. The prompt should describe the desired final scene. "
            "INTAKE REQUIRED: confirm aspect_ratio AND quality with the user "
            "before calling — composite produces a NEW image, not source-shaped."
        )
    )
    async def composite_images(
        prompt: Annotated[str, Field(min_length=1, max_length=4000,
            description="What the final scene should look like. Reference inputs by index (e.g. 'place subject from image 1 onto background from image 2').")],
        images: Annotated[list[str], Field(min_length=2, max_length=16,
            description="2–16 source images (URL or asset_id).")],
        aspect_ratio: Annotated[str, Field(description="Confirm with user. 1:1, 4:5, 16:9, 9:16, 3:2, 2:3, etc.")] = "1:1",
        quality: Annotated[str, Field(description="Confirm with user. low | medium (default) | high (4K, costs more).")] = "medium",
        count: Annotated[int, Field(ge=1, le=4)] = 1,
        output_format: Annotated[str, Field()] = "png",
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        if len(images) < 2:
            raise ValueError("composite_images requires at least 2 source images. Use edit_image for single-source edits.")
        sources: list[tuple[bytes, str]] = []
        for ref in images:
            b, m = resolve_image_input(ref)
            sources.append((b, m))
        size = size_for_aspect(aspect_ratio, prefer_high=(quality == "high"))
        result_images, usage = await call_edit(
            prompt=prompt,
            images=sources,
            mask=None,
            size=size,
            n=count,
            quality=quality,
            output_format=output_format,
        )
        mime = _output_format_to_mime(output_format)
        cost_meta = attach_cost_meta(
            usage=usage, quality=quality, n=count,
            has_image_input=True, prompt_chars=len(prompt), attempt_count=1,
        )
        return await _build_response(result_images, mime=mime, cost_meta=cost_meta,
                               save_folder=save_folder, prefix="composite")

    @mcp.tool(
        description=(
            "Add a text overlay to an existing image. DETERMINISTIC by default — "
            "renders via Pillow with zero API cost and exact text (no AI). "
            "Pass render_mode='ai_integrated' only when you need the text physically "
            "integrated into the scene (e.g., on a sign within the image); the AI path "
            "may not be character-perfect. position is a preset name OR pass an explicit "
            "bbox={x,y,w,h} in [0,1]."
        )
    )
    async def add_text_overlay(
        image: Annotated[str, Field(description="URL or asset_id")],
        text: Annotated[str, Field(min_length=1, max_length=400)],
        position: Annotated[Optional[str], Field(description="top|bottom|center|top-left|... (or pass bbox)")] = None,
        bbox: Annotated[Optional[dict], Field(description="{x,y,w,h} in [0,1]; overrides position")] = None,
        style: Annotated[str, Field(description="headline|badge|caption|watermark")] = "headline",
        color: Annotated[Optional[str], Field(description="hex like #ffffff or named")] = None,
        render_mode: Annotated[str, Field(description="deterministic|ai_integrated")] = "deterministic",
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        src_bytes, _src_mime = resolve_image_input(image)

        if render_mode == "deterministic":
            out = render_text_overlay(
                src_bytes, text=text, position=position, bbox=bbox, style=style, color=color,
            )
            cost_meta = attach_cost_meta(
                usage={}, quality="n/a", n=1, has_image_input=True,
                prompt_chars=len(text), attempt_count=1,
            )
            cost_meta["estimated_cost_usd"] = 0.0
            cost_meta["actual_cost_usd"] = 0.0
            return await _build_response([out], mime="image/png", cost_meta=cost_meta,
                                   save_folder=save_folder, prefix="overlay")

        if render_mode == "ai_integrated":
            ai_prompt = text_overlay_ai(text, position=position or "bottom-center", style=style, color=color)
            base_w, base_h = get_image_dimensions(src_bytes)
            size = _closest_aspect_size(base_w, base_h)
            images, usage = await call_edit(
                prompt=ai_prompt,
                images=[(src_bytes, "image/png")],
                size=size, n=1, quality="medium", output_format="png",
            )
            cost_meta = attach_cost_meta(
                usage=usage, quality="medium", n=1, has_image_input=True,
                prompt_chars=len(ai_prompt), attempt_count=1,
            )
            return await _build_response(images, mime="image/png", cost_meta=cost_meta,
                                   save_folder=save_folder, prefix="overlay-ai")

        raise ValueError(f"Unknown render_mode '{render_mode}'. Use 'deterministic' or 'ai_integrated'.")

    @mcp.tool(
        description=(
            "Add a CTA + URL to an image. DETERMINISTIC by default — URL accuracy is "
            "critical, so the URL string is rendered character-for-character via Pillow "
            "with zero API cost. Use render_mode='ai_integrated' only for stylized "
            "in-scene CTAs where exact URL fidelity is not required (rarely the case)."
        )
    )
    async def add_link_cta(
        image: Annotated[str, Field(description="URL or asset_id")],
        cta_text: Annotated[str, Field(max_length=80, description="e.g. 'Shop Now'. Empty string for URL-only.")] = "",
        url_text: Annotated[str, Field(min_length=1, max_length=200, description="EXACT URL text — every character must match.")] = ...,
        position: Annotated[Optional[str], Field()] = "bottom-center",
        bbox: Annotated[Optional[dict], Field()] = None,
        style: Annotated[str, Field(description="button|bar|pill|underlined-link")] = "button",
        color_scheme: Annotated[str, Field(description="white-on-coral|coral-on-white|white-on-black|...")] = "white-on-coral",
        render_mode: Annotated[str, Field(description="deterministic|ai_integrated")] = "deterministic",
        save_folder: Annotated[Optional[str], Field()] = None,
    ) -> list:
        src_bytes, _src_mime = resolve_image_input(image)

        if render_mode == "deterministic":
            out = render_link_cta(
                src_bytes, cta_text=cta_text, url_text=url_text,
                position=position, bbox=bbox, style=style, color_scheme=color_scheme,
            )
            cost_meta = attach_cost_meta(
                usage={}, quality="n/a", n=1, has_image_input=True,
                prompt_chars=len(cta_text) + len(url_text), attempt_count=1,
            )
            cost_meta["estimated_cost_usd"] = 0.0
            cost_meta["actual_cost_usd"] = 0.0
            return await _build_response([out], mime="image/png", cost_meta=cost_meta,
                                   save_folder=save_folder, prefix="cta")

        if render_mode == "ai_integrated":
            ai_prompt = link_cta_ai(
                cta_text, url_text, position=position or "bottom-center",
                style=style, color_scheme=color_scheme,
            )
            base_w, base_h = get_image_dimensions(src_bytes)
            size = _closest_aspect_size(base_w, base_h)
            images, usage = await call_edit(
                prompt=ai_prompt,
                images=[(src_bytes, "image/png")],
                size=size, n=1, quality="medium", output_format="png",
            )
            cost_meta = attach_cost_meta(
                usage=usage, quality="medium", n=1, has_image_input=True,
                prompt_chars=len(ai_prompt), attempt_count=1,
            )
            return await _build_response(images, mime="image/png", cost_meta=cost_meta,
                                   save_folder=save_folder, prefix="cta-ai")

        raise ValueError(f"Unknown render_mode '{render_mode}'. Use 'deterministic' or 'ai_integrated'.")


def _looks_like_b64(s: str) -> bool:
    if len(s) < 200:
        return False
    head = s[:200]
    import re
    return bool(re.match(r"^[A-Za-z0-9+/=_-]+$", head))


def _closest_aspect_size(w: int, h: int, *, prefer_high: bool = False) -> str:
    """Pick the gpt-image-2 aspect alias closest to a source image's ratio."""
    if w <= 0 or h <= 0:
        return "2048x2048" if prefer_high else "1024x1024"
    ratio = w / h
    candidates = [
        ("1:1", 1.0),
        ("4:5", 0.8),  ("5:4", 1.25),
        ("2:3", 2 / 3), ("3:2", 1.5),
        ("9:16", 9 / 16), ("16:9", 16 / 9),
    ]
    alias = min(candidates, key=lambda c: abs(c[1] - ratio))[0]
    return size_for_aspect(alias, prefer_high=prefer_high)
