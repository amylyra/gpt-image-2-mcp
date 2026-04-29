"""HMAC URL signing for asset_id-based image URLs.

Browsers don't carry Authorization headers on <img> tags, so we sign URLs with HMAC
instead of bearer tokens. The signature lives in the query string and is verified
on every GET /images/{asset_id} request.

Format: /images/{asset_id}?expires=<unix_ts>&sig=<hex>
  sig = hmac_sha256(IMAGE_URL_SIGNING_KEY, f"{asset_id}|{expires}").hexdigest()[:32]

Behavior:
  - 404 on bad sig (don't leak existence)
  - 410 on expired
  - constant-time compare via hmac.compare_digest

v0.2 will add bearer + rate-limit hooks for mutating endpoints.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time


_SIG_LEN = 32  # 32 hex chars = 128 bits — plenty


def _get_key() -> bytes:
    key = os.environ.get("IMAGE_URL_SIGNING_KEY", "")
    if not key:
        raise RuntimeError(
            "IMAGE_URL_SIGNING_KEY env var is required. "
            "Generate one with: openssl rand -hex 32"
        )
    return key.encode("utf-8")


def sign_url(asset_id: str, expires: int) -> str:
    """Return the HMAC-SHA256 hex signature (truncated to 32 chars)."""
    key = _get_key()
    msg = f"{asset_id}|{expires}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:_SIG_LEN]


def verify_url(asset_id: str, expires: int, sig: str) -> tuple[bool, str]:
    """Verify a signed URL.

    Returns (ok, reason) where reason is one of "ok", "expired", "bad_sig".
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not sig or len(sig) != _SIG_LEN:
        return False, "bad_sig"
    expected = sign_url(asset_id, expires)
    if not hmac.compare_digest(expected, sig):
        return False, "bad_sig"
    if int(time.time()) > expires:
        return False, "expired"
    return True, "ok"


# ---------------------------------------------------------------------------
# Bearer auth for mutating HTTP endpoints (v0.2)
#
# Why HMAC for /images/{id} but bearer for /upload:
#   - /images/{id} is consumed by browsers in <img src=...> (no headers).
#   - /upload is called programmatically; bearer is the standard guard.
# Both stay opt-in: if MCP_BEARER_TOKEN is unset, the endpoint is open
# (acceptable for single-tenant trusted deployments).
# ---------------------------------------------------------------------------
def bearer_required() -> bool:
    return bool(os.environ.get("MCP_BEARER_TOKEN", "").strip())


def check_bearer(authorization_header: str | None) -> tuple[bool, str]:
    """Constant-time check against MCP_BEARER_TOKEN.

    Returns (ok, reason). reason: "ok" | "missing" | "bad_scheme" | "wrong_token" | "not_required".
    """
    expected = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if not expected:
        return True, "not_required"
    if not authorization_header:
        return False, "missing"
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False, "bad_scheme"
    token = parts[1].strip()
    if not hmac.compare_digest(expected, token):
        return False, "wrong_token"
    return True, "ok"


# ---------------------------------------------------------------------------
# In-app rate limit (single-instance safety net) — v1.0
#
# This is NOT a substitute for Cloud Armor / Redis-backed limits. It only
# protects a single process against accidental floods. Multi-instance Cloud
# Run deploys should use Cloud Armor at the edge instead (see CLAUDE.md).
#
# Defaults: OFF. Set RATE_LIMIT_PER_MINUTE=N to enable a token bucket of
# N per IP per minute against mutating endpoints (/upload, /api/{tool}).
# ---------------------------------------------------------------------------
import time
from collections import defaultdict
from threading import Lock

_BUCKETS: dict[str, list[float]] = defaultdict(list)
_BUCKETS_LOCK = Lock()


def rate_limit_per_minute() -> int:
    raw = os.environ.get("RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return 0
    try:
        n = int(raw)
    except ValueError:
        return 0
    return max(0, n)


def check_rate_limit(client_id: str) -> tuple[bool, int]:
    """Sliding-window count over the last 60s.

    Returns (allowed, retry_after_seconds). When unconfigured (env unset or 0),
    always allowed. Bucket is keyed by client_id (typically the source IP) so
    behind a load balancer the caller must pass the resolved IP, not the LB IP.
    """
    cap = rate_limit_per_minute()
    if cap <= 0:
        return True, 0
    now = time.time()
    cutoff = now - 60.0
    with _BUCKETS_LOCK:
        bucket = _BUCKETS[client_id]
        # Drop hits older than the window.
        i = 0
        while i < len(bucket) and bucket[i] < cutoff:
            i += 1
        if i:
            del bucket[:i]
        if len(bucket) >= cap:
            # Oldest hit + 60s = when the next slot opens.
            return False, max(1, int(60 - (now - bucket[0])))
        bucket.append(now)
        return True, 0


def reset_rate_limits() -> None:
    """Clear all buckets — for tests."""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


def client_ip_from_request(request) -> str:
    """Best-effort source-IP extraction.

    On Cloud Run / behind a proxy, X-Forwarded-For is the trustworthy
    field (the platform sets it). Falls back to the socket peer.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # First entry is the original client; the rest are proxy hops.
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"
