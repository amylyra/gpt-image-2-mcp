"""Asset storage with HMAC-signed URLs.

v0.1: in-memory dict with TTL eviction (dev-only — restart-volatile).
v0.2: S3/GCS hooks via upload_to_cloud(); same interface so callers don't change.

Each stored image gets:
  - asset_id  — opaque, prefixed "img_", URL-safe (the durable handle)
  - signed image_url — /images/{asset_id}?expires=...&sig=... (default 24h TTL)

Tools accept either an http(s) URL or an asset_id; image_io.resolve_image_input
routes between them.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from security import sign_url


# ---------------------------------------------------------------------------
# In-memory store (v0.1 dev only — swapped for GCS/S3 in v0.2)
# ---------------------------------------------------------------------------
_STORE: dict[str, tuple[bytes, str, float]] = {}  # asset_id -> (bytes, mime, ts)
_LOCK = threading.Lock()
_STORE_MAX_ITEMS = int(os.environ.get("STORE_MAX_ITEMS", 200))
_STORE_TTL = int(os.environ.get("STORE_TTL", 86400))  # 24h matches default URL TTL

# Default URL signing TTL — clamps signed URL lifetimes regardless of caller intent.
DEFAULT_URL_TTL = int(os.environ.get("IMAGE_URL_TTL_SECONDS", 86400))


# ---------------------------------------------------------------------------
# asset_id generation
# ---------------------------------------------------------------------------
def make_asset_id() -> str:
    """Opaque, URL-safe asset id. Prefixed so callers can route on shape."""
    return "img_" + secrets.token_urlsafe(16).rstrip("-_") + secrets.token_hex(2)


# ---------------------------------------------------------------------------
# Storage operations
# ---------------------------------------------------------------------------
def _gc() -> None:
    now = time.time()
    with _LOCK:
        expired = [k for k, (_, _, ts) in _STORE.items() if now - ts > _STORE_TTL]
        for k in expired:
            del _STORE[k]


def store_asset(img_bytes: bytes, mime: str, *, asset_id: Optional[str] = None) -> str:
    """Store image bytes; return asset_id."""
    _gc()
    aid = asset_id or make_asset_id()
    with _LOCK:
        while len(_STORE) >= _STORE_MAX_ITEMS:
            oldest = min(_STORE, key=lambda k: _STORE[k][2])
            del _STORE[oldest]
        _STORE[aid] = (img_bytes, mime, time.time())
    return aid


def resolve_asset(asset_id: str) -> tuple[bytes, str]:
    """Fetch (bytes, mime) for an asset_id. Raises KeyError if missing/expired."""
    with _LOCK:
        entry = _STORE.get(asset_id)
    if not entry:
        raise KeyError(
            f"Asset '{asset_id}' not found. It may have expired "
            f"(TTL={_STORE_TTL}s) or the server restarted (in-memory store, v0.1)."
        )
    return entry[0], entry[1]


def has_asset(asset_id: str) -> bool:
    with _LOCK:
        return asset_id in _STORE


# ---------------------------------------------------------------------------
# Base URL + signed URL construction
# ---------------------------------------------------------------------------
def _compute_base_url() -> str:
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if base:
        return base
    port = int(os.environ.get("PORT", 8080))
    return f"http://localhost:{port}"


_BASE_URL = _compute_base_url()


def get_base_url() -> str:
    return _BASE_URL


def make_signed_url(
    asset_id: str, *, ttl_seconds: Optional[int] = None
) -> tuple[str, datetime]:
    """Build a /images/{asset_id}?expires=...&sig=... URL and its expiry datetime."""
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_URL_TTL
    expires = int(time.time()) + ttl
    sig = sign_url(asset_id, expires)
    url = f"{_BASE_URL}/images/{asset_id}?expires={expires}&sig={sig}"
    expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
    return url, expires_at


# ---------------------------------------------------------------------------
# Cloud storage (v0.2)
# ---------------------------------------------------------------------------
S3_BUCKET = os.environ.get("S3_BUCKET", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
S3_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
S3_PREFIX = os.environ.get("S3_PREFIX", "gpt-image-2")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "gpt-image-2")


def cloud_storage_configured() -> bool:
    return bool(S3_BUCKET or GCS_BUCKET)


def _ext_for_mime(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "bin")


def _upload_s3_sync(img_bytes: bytes, mime: str, *, asset_id: str) -> str:
    import boto3  # imported lazily — boto3 is only needed when S3_BUCKET is set
    s3 = boto3.client("s3", region_name=S3_REGION)
    key = f"{S3_PREFIX}/{asset_id}.{_ext_for_mime(mime)}"
    s3.put_object(
        Bucket=S3_BUCKET, Key=key, Body=img_bytes, ContentType=mime,
        CacheControl="public, max-age=31536000, immutable",
    )
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


def _upload_gcs_sync(img_bytes: bytes, mime: str, *, asset_id: str) -> str:
    from google.cloud import storage as gcs  # lazy import
    client = gcs.Client()
    bucket = client.bucket(GCS_BUCKET)
    key = f"{GCS_PREFIX}/{asset_id}.{_ext_for_mime(mime)}"
    blob = bucket.blob(key)
    blob.cache_control = "public, max-age=31536000, immutable"
    blob.upload_from_string(img_bytes, content_type=mime)
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{key}"


async def upload_to_cloud(img_bytes: bytes, mime: str, *, asset_id: str) -> Optional[str]:
    """Upload to S3/GCS if configured; return durable public URL or None.

    Preference: S3 first if both are set (mirrors nanobanana behavior).
    Falls through to None on any error so callers can fall back to in-memory store
    unless REQUIRE_DURABLE_UPLOADS is set, in which case the caller decides.
    """
    import asyncio
    if S3_BUCKET:
        try:
            return await asyncio.to_thread(_upload_s3_sync, img_bytes, mime, asset_id=asset_id)
        except Exception:
            return None
    if GCS_BUCKET:
        try:
            return await asyncio.to_thread(_upload_gcs_sync, img_bytes, mime, asset_id=asset_id)
        except Exception:
            return None
    return None
