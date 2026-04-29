"""Image I/O: URL fetch (with SSRF guards), normalization, mask validation, asset routing.

Adapted from nanobanana-mcp/server.py — same SSRF rules, same error messages,
same Drive rewrite. Public surface:

  - is_url(s)
  - is_asset_id(s)
  - fetch_url(url) -> (bytes, mime)
  - resolve_image_input(ref) -> (bytes, mime)   [URL or asset_id]
  - normalize_image(bytes, *, max_dim, target_format) -> (bytes, mime)
  - validate_mask(mask_bytes, image_w, image_h) -> bytes (PNG with alpha)
"""

from __future__ import annotations

import base64
import os
import re
from io import BytesIO
from urllib.parse import urlparse

import httpx

from storage import has_asset, resolve_asset


MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", 50 * 1024 * 1024))  # 50MB per gpt-image-2 limits
MAX_MASK_BYTES = 50 * 1024 * 1024
FETCH_RETRIES = int(os.environ.get("FETCH_RETRIES", 1))


# ---------------------------------------------------------------------------
# Type predicates
# ---------------------------------------------------------------------------
def is_url(s: str) -> bool:
    try:
        parsed = urlparse(s)
        return parsed.scheme in ("http", "https")
    except Exception:
        return False


def is_asset_id(s: str) -> bool:
    return isinstance(s, str) and s.startswith("img_") and len(s) <= 80


# ---------------------------------------------------------------------------
# URL normalization (Drive share-page rewrite)
# ---------------------------------------------------------------------------
def normalize_share_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname in ("drive.google.com", "docs.google.com"):
        m = re.search(r"/file/d/([^/?]+)", parsed.path)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        if "id" in params:
            return f"https://drive.google.com/uc?export=download&id={params['id']}"
    return url


# ---------------------------------------------------------------------------
# URL fetch with SSRF guards
# ---------------------------------------------------------------------------
_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "169.254.169.254",
    "100.100.100.200",
}


def _check_image_size(body: bytes, source: str = "image") -> None:
    if len(body) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{source} is too large ({len(body) / (1024 * 1024):.1f}MB). "
            f"Maximum allowed size is {MAX_IMAGE_BYTES // (1024 * 1024)}MB."
        )


def fetch_url(url: str) -> tuple[bytes, str]:
    """Fetch an http(s) URL with SSRF protection and size limits."""
    url = normalize_share_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTS or hostname.startswith("169.254."):
        raise ValueError(f"Blocked request to internal address: {hostname}")
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError(f"Blocked request to localhost: {hostname}")

    body = b""
    content_type = "image/jpeg"
    for attempt in range(FETCH_RETRIES + 1):
        try:
            timeout = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=20.0)
            with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_IMAGE_BYTES:
                            raise ValueError(
                                f"Fetched image is too large ({int(content_length) / (1024 * 1024):.1f}MB). "
                                f"Maximum allowed size is {MAX_IMAGE_BYTES // (1024 * 1024)}MB."
                            )
                    except ValueError:
                        raise
                    except Exception:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"Fetched image is too large ({total / (1024 * 1024):.1f}MB). "
                            f"Maximum allowed size is {MAX_IMAGE_BYTES // (1024 * 1024)}MB."
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                hint = (
                    " If this is a Google Drive link, share it with 'Anyone with the link', "
                    "or upload via the /upload endpoint."
                ) if "drive.google.com" in url else ""
                raise ValueError(f"Image URL returned 403 (forbidden).{hint}")
            if e.response.status_code == 404:
                raise ValueError(
                    "Image URL returned 404. The image may have been deleted or the URL is wrong."
                )
            raise ValueError(f"Failed to fetch image from URL: {e}")
        except ValueError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt < FETCH_RETRIES:
                continue
            raise ValueError(f"Timed out or network error fetching image URL: {e}")

    if content_type == "text/html":
        if "drive.google.com" in url:
            raise ValueError(
                "Google Drive returned an HTML page instead of the image file. "
                "For files >25MB, open the link in a browser and use the post-confirm download URL."
            )
        raise ValueError(
            "URL returned an HTML page instead of an image. "
            "Check that the URL points directly to an image file."
        )
    _check_image_size(body, source="Fetched image")
    return body, content_type


# ---------------------------------------------------------------------------
# Unified input resolver — URL, asset_id, or our own /images/{id} URL
# ---------------------------------------------------------------------------
_DATA_URI_REJECT_MSG = (
    "Base64 strings and data: URIs are not accepted as tool parameters — they're too "
    "large for MCP transport and will hang the connection. "
    "Upload the image first via upload_image, or POST to /upload to get an asset_id / image_url."
)


def resolve_image_input(ref: str) -> tuple[bytes, str]:
    """Resolve a tool parameter to (bytes, mime).

    Accepts:
      - asset_id (img_...) — fetched from store
      - http(s) URL — fetched (our own /images/{id} URLs are routed to the store)

    Rejects:
      - base64 / data: URIs (use upload_image first)
      - other schemes (gs://, s3://, file://, ftp://)
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError("Image reference must be a non-empty string.")

    if is_asset_id(ref):
        try:
            return resolve_asset(ref)
        except KeyError as e:
            raise ValueError(str(e))

    if ref.startswith("data:") or _looks_like_base64(ref):
        raise ValueError(_DATA_URI_REJECT_MSG)

    if is_url(ref):
        parsed = urlparse(ref)
        if parsed.path.startswith("/images/"):
            aid = parsed.path.removeprefix("/images/")
            if has_asset(aid):
                return resolve_asset(aid)
            raise ValueError(
                f"Asset '{aid}' has expired or was evicted from the server store. "
                "Upload again to get a fresh URL."
            )
        return fetch_url(ref)

    parsed = urlparse(ref)
    if parsed.scheme and parsed.scheme not in ("http", "https") and len(parsed.scheme) > 1:
        raise ValueError(
            f"Unsupported URL scheme '{parsed.scheme}://'. "
            "Pass an http/https URL or an asset_id (img_...). "
            "Use upload_image to convert a local file or pasted image into one."
        )

    raise ValueError(
        "Image reference is not an http(s) URL or asset_id. "
        "Use upload_image to obtain one."
    )


def _looks_like_base64(s: str) -> bool:
    """Heuristic: long string of base64-only chars with no URL/path delimiters."""
    if len(s) < 100:
        return False
    if any(c in s[:200] for c in (" ", ":", "?", "&", "\\")):
        return False
    return bool(re.match(r"^[A-Za-z0-9+/=_-]+$", s[:200]))


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_image(
    img_bytes: bytes, *, max_dim: int = 2048, target_format: str = "PNG", quality: int = 90
) -> tuple[bytes, str]:
    """Decode, optionally resize, re-encode. Returns (bytes, mime)."""
    from PIL import Image as PILImage

    try:
        img = PILImage.open(BytesIO(img_bytes))
        w, h = img.size
        if w < 1 or h < 1:
            raise ValueError("Image has invalid dimensions; data may be corrupt.")

        if target_format.upper() == "JPEG":
            if img.mode == "RGBA":
                bg = PILImage.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)

        buf = BytesIO()
        fmt = target_format.upper()
        if fmt == "PNG":
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png"
        if fmt == "JPEG":
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue(), "image/jpeg"
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=quality)
            return buf.getvalue(), "image/webp"
        raise ValueError(f"Unsupported target_format: {target_format}")
    except ValueError:
        raise
    except Exception:
        raise ValueError(
            "Could not decode image data. The image may be corrupt or truncated."
        )


def get_image_dimensions(img_bytes: bytes) -> tuple[int, int]:
    from PIL import Image as PILImage
    img = PILImage.open(BytesIO(img_bytes))
    return img.size


# ---------------------------------------------------------------------------
# Mask validation (gpt-image-2 edits)
# Rules:
#   - PNG with alpha channel
#   - Same dimensions as base image
#   - <= 50MB
#   - If user passed black/white-only PNG, synthesize alpha from luminance
# ---------------------------------------------------------------------------
def validate_mask(mask_bytes: bytes, image_w: int, image_h: int) -> bytes:
    from PIL import Image as PILImage

    if len(mask_bytes) > MAX_MASK_BYTES:
        raise ValueError(
            f"Mask is too large ({len(mask_bytes) / (1024 * 1024):.1f}MB). "
            f"Maximum allowed size is {MAX_MASK_BYTES // (1024 * 1024)}MB."
        )

    try:
        mask = PILImage.open(BytesIO(mask_bytes))
    except Exception:
        raise ValueError("Could not decode mask. Mask must be a valid PNG.")

    if mask.size != (image_w, image_h):
        raise ValueError(
            f"Mask dimensions {mask.size} do not match base image ({image_w}x{image_h}). "
            "Resize the mask to match before passing."
        )

    # If mask already has alpha, re-emit as PNG (some uploads use other formats).
    if mask.mode in ("RGBA", "LA"):
        buf = BytesIO()
        mask.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    # Synthesize alpha from luminance (white = keep, black = edit).
    gray = mask.convert("L")
    rgba = PILImage.new("RGBA", mask.size, (0, 0, 0, 0))
    rgba.putalpha(gray)
    buf = BytesIO()
    rgba.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
