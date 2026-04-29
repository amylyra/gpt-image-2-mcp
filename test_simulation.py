"""Mocked-OpenAI test suite for gpt-image-2-mcp v0.1.

Run: python -m pytest test_simulation.py -x -q

Sets stub env vars at import time so storage/security modules import cleanly.
"""

from __future__ import annotations

import base64
import os
import time
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub env BEFORE importing modules that read it at import time.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("IMAGE_URL_SIGNING_KEY", "0" * 64)
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

from PIL import Image as PILImage  # noqa: E402

import image_io  # noqa: E402
import openai_images  # noqa: E402
import storage  # noqa: E402
from costs import actual_cost_from_usage, attach_cost_meta, estimate_cost  # noqa: E402
from deterministic_render import render_link_cta, render_text_overlay  # noqa: E402
from openai_images import (  # noqa: E402
    parse_size,
    size_for_aspect,
    validate_image_params,
    validate_size,
)
from security import bearer_required, check_bearer, sign_url, verify_url  # noqa: E402
from storage import (  # noqa: E402
    has_asset,
    make_asset_id,
    make_signed_url,
    resolve_asset,
    store_asset,
)
from tools import unpack_tool_result  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _png(w: int = 64, h: int = 64, color=(255, 0, 0, 255)) -> bytes:
    img = PILImage.new("RGBA", (w, h), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _b64_png(w: int = 64, h: int = 64) -> str:
    return base64.b64encode(_png(w, h)).decode("ascii")


def _fake_response(images: list[bytes], usage: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.data = [MagicMock(b64_json=base64.b64encode(b).decode()) for b in images]
    resp.usage = usage or {
        "input_tokens": 100,
        "output_tokens": 1100,
        "input_tokens_details": {"text_tokens": 100, "image_tokens": 0},
        "output_tokens_details": {"image_tokens": 1100},
    }
    return resp


# ---------------------------------------------------------------------------
# Storage + signed URLs
# ---------------------------------------------------------------------------
class TestImageStore:
    def test_store_and_resolve(self):
        b = _png()
        aid = store_asset(b, "image/png")
        assert aid.startswith("img_")
        assert has_asset(aid)
        out, mime = resolve_asset(aid)
        assert out == b
        assert mime == "image/png"

    def test_make_asset_id_unique(self):
        ids = {make_asset_id() for _ in range(50)}
        assert len(ids) == 50

    def test_resolve_missing_raises(self):
        with pytest.raises(KeyError):
            resolve_asset("img_does_not_exist")


class TestSignedUrls:
    def test_sign_verify_roundtrip(self):
        aid = "img_abc123"
        expires = int(time.time()) + 3600
        sig = sign_url(aid, expires)
        ok, reason = verify_url(aid, expires, sig)
        assert ok and reason == "ok"

    def test_tampered_sig_rejected(self):
        aid = "img_abc123"
        expires = int(time.time()) + 3600
        sig = sign_url(aid, expires)
        bad = ("0" if sig[0] != "0" else "1") + sig[1:]
        ok, reason = verify_url(aid, expires, bad)
        assert not ok and reason == "bad_sig"

    def test_expired_rejected(self):
        aid = "img_abc123"
        expires = int(time.time()) - 1
        sig = sign_url(aid, expires)
        ok, reason = verify_url(aid, expires, sig)
        assert not ok and reason == "expired"

    def test_make_signed_url_returns_iso(self):
        aid = store_asset(_png(), "image/png")
        url, expires_at = make_signed_url(aid, ttl_seconds=60)
        assert "expires=" in url and "sig=" in url
        assert expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Image normalization
# ---------------------------------------------------------------------------
class TestNormalizeImage:
    def test_rgb_to_png(self):
        img = PILImage.new("RGB", (32, 32), (10, 20, 30))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        out, mime = image_io.normalize_image(buf.getvalue(), max_dim=2048, target_format="PNG")
        assert mime == "image/png"
        PILImage.open(BytesIO(out))  # decodes

    def test_rgba_to_png_preserves_alpha(self):
        b = _png(w=10, h=10, color=(0, 0, 0, 0))
        out, _ = image_io.normalize_image(b, max_dim=2048, target_format="PNG")
        assert PILImage.open(BytesIO(out)).mode == "RGBA"

    def test_oversized_resized(self):
        img = PILImage.new("RGB", (4096, 2048), (255, 255, 255))
        buf = BytesIO(); img.save(buf, format="PNG")
        out, _ = image_io.normalize_image(buf.getvalue(), max_dim=2048, target_format="PNG")
        w, h = PILImage.open(BytesIO(out)).size
        assert max(w, h) == 2048


# ---------------------------------------------------------------------------
# Aspect-ratio shim — RATIOS ARE PRESERVED across tiers (regression)
# ---------------------------------------------------------------------------
class TestSizeShim:
    def test_aliases_resolve(self):
        for alias in ("1:1", "16:9", "9:16", "infographic-portrait", "social-vertical"):
            s = size_for_aspect(alias)
            parse_size(s)  # no exception

    def test_ratio_preserved_default_vs_high(self):
        """The known v3 plan bug: 16:9 default → 16:9 high (not 3:2). Don't regress."""
        for alias in ("16:9", "9:16", "3:2", "2:3", "4:5", "5:4"):
            d_w, d_h = parse_size(size_for_aspect(alias, prefer_high=False))
            h_w, h_h = parse_size(size_for_aspect(alias, prefer_high=True))
            r_default = d_w / d_h
            r_high = h_w / h_h
            assert abs(r_default - r_high) < 0.02, f"{alias}: ratio drifts {r_default} -> {r_high}"

    def test_4k_opt_in_only(self):
        d_w, d_h = parse_size(size_for_aspect("16:9", prefer_high=False))
        h_w, h_h = parse_size(size_for_aspect("16:9", prefer_high=True))
        assert max(d_w, d_h) <= 2048
        assert max(h_w, h_h) > 2048

    def test_unknown_alias_raises(self):
        with pytest.raises(ValueError):
            size_for_aspect("hexagon")


class TestValidateSize:
    def test_valid_passes(self):
        validate_size(1024, 1024)
        validate_size(2048, 1152)
        validate_size(1024, 1280)

    def test_max_edge(self):
        with pytest.raises(ValueError):
            validate_size(4096, 1024)

    def test_min_pixels(self):
        with pytest.raises(ValueError):
            validate_size(512, 512)

    def test_max_pixels(self):
        with pytest.raises(ValueError):
            validate_size(3840, 3840)

    def test_ratio(self):
        with pytest.raises(ValueError):
            validate_size(2048, 512)

    def test_multiple_of_16(self):
        with pytest.raises(ValueError):
            validate_size(1023, 1024)


# ---------------------------------------------------------------------------
# Unsupported-param contract test
# ---------------------------------------------------------------------------
class TestNoUnsupportedParams:
    def test_response_format_rejected(self):
        with pytest.raises(ValueError, match="response_format"):
            validate_image_params(size="1024x1024", response_format="b64_json")

    def test_thinking_rejected(self):
        with pytest.raises(ValueError, match="thinking"):
            validate_image_params(size="1024x1024", thinking={"effort": "high"})

    def test_input_fidelity_rejected(self):
        with pytest.raises(ValueError, match="input_fidelity"):
            validate_image_params(size="1024x1024", input_fidelity="high")

    def test_transparent_background_rejected(self):
        with pytest.raises(ValueError, match="transparent"):
            validate_image_params(size="1024x1024", background="transparent")

    def test_compression_with_png_rejected(self):
        with pytest.raises(ValueError, match="compression"):
            validate_image_params(size="1024x1024", output_format="png", output_compression=80)

    @pytest.mark.asyncio
    async def test_call_generate_does_not_pass_unsupported(self):
        captured = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return _fake_response([_png()])

        with patch.object(openai_images, "get_client", return_value=MagicMock(images=MagicMock(generate=fake_generate))):
            await openai_images.call_generate(prompt="x", size="1024x1024", n=1, quality="low")

        for banned in ("response_format", "thinking", "input_fidelity"):
            assert banned not in captured


# ---------------------------------------------------------------------------
# Resolve image input — URL or asset_id
# ---------------------------------------------------------------------------
class TestAssetIdRouting:
    def test_resolves_asset_id(self):
        aid = store_asset(_png(), "image/png")
        b, mime = image_io.resolve_image_input(aid)
        assert mime == "image/png"
        assert len(b) > 0

    def test_rejects_data_uri(self):
        with pytest.raises(ValueError, match="Base64"):
            image_io.resolve_image_input("data:image/png;base64,iVBORw0KGgo")

    def test_rejects_pure_b64_string(self):
        with pytest.raises(ValueError, match="Base64"):
            image_io.resolve_image_input(_b64_png(64, 64))

    def test_rejects_unknown_scheme(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            image_io.resolve_image_input("gs://bucket/key")

    def test_routes_own_images_url_to_store(self):
        aid = store_asset(_png(), "image/png")
        url = f"http://localhost:8080/images/{aid}"
        b, mime = image_io.resolve_image_input(url)
        assert mime == "image/png"
        assert len(b) > 0


# ---------------------------------------------------------------------------
# Mask validation
# ---------------------------------------------------------------------------
class TestMaskValidation:
    def test_alpha_mask_passes(self):
        m = _png(64, 64, color=(0, 0, 0, 128))
        out = image_io.validate_mask(m, 64, 64)
        assert PILImage.open(BytesIO(out)).mode in ("RGBA", "LA")

    def test_dimension_mismatch_rejected(self):
        m = _png(32, 32)
        with pytest.raises(ValueError, match="dimensions"):
            image_io.validate_mask(m, 64, 64)

    def test_bw_mask_synthesizes_alpha(self):
        bw = PILImage.new("L", (64, 64), 128)
        buf = BytesIO(); bw.save(buf, format="PNG")
        out = image_io.validate_mask(buf.getvalue(), 64, 64)
        assert PILImage.open(BytesIO(out)).mode == "RGBA"


# ---------------------------------------------------------------------------
# Deterministic rendering — exact text, zero API calls
# ---------------------------------------------------------------------------
class TestDeterministicRender:
    def test_text_overlay_returns_png(self):
        out = render_text_overlay(_png(512, 512), text="Spring Sale", style="headline")
        assert out[:8] == b"\x89PNG\r\n\x1a\n"
        PILImage.open(BytesIO(out))

    def test_text_overlay_zero_api_calls(self):
        with patch.object(openai_images, "get_client") as mock_client:
            render_text_overlay(_png(256, 256), text="x", style="badge")
            mock_client.assert_not_called()

    def test_link_cta_returns_png(self):
        out = render_link_cta(
            _png(800, 600), cta_text="Shop Now", url_text="proven.com/spring",
            position="bottom-center", style="button", color_scheme="white-on-coral",
        )
        assert out[:8] == b"\x89PNG\r\n\x1a\n"

    def test_link_cta_zero_api_calls(self):
        with patch.object(openai_images, "get_client") as mock_client:
            render_link_cta(
                _png(800, 600), cta_text="Buy", url_text="example.com/x",
                style="bar", color_scheme="white-on-black",
            )
            mock_client.assert_not_called()

    def test_link_cta_requires_url(self):
        with pytest.raises(ValueError, match="url_text"):
            render_link_cta(_png(), cta_text="Shop", url_text="", style="button", color_scheme="white-on-coral")

    def test_text_overlay_unknown_style(self):
        with pytest.raises(ValueError, match="style"):
            render_text_overlay(_png(), text="x", style="hexagon")

    def test_bbox_accepted(self):
        out = render_text_overlay(
            _png(512, 512), text="x", style="caption",
            bbox={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.2},
        )
        assert out[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Generate / edit calls (mocked SDK)
# ---------------------------------------------------------------------------
class TestGenerateImage:
    @pytest.mark.asyncio
    async def test_basic_generate(self):
        client = MagicMock()
        client.images.generate = MagicMock(return_value=_fake_response([_png()]))
        with patch.object(openai_images, "get_client", return_value=client):
            images, usage = await openai_images.call_generate(
                prompt="a cat", size="1024x1024", n=1, quality="low",
            )
        assert len(images) == 1
        assert usage["output_tokens"] == 1100

    @pytest.mark.asyncio
    async def test_count_returns_n_variants(self):
        client = MagicMock()
        client.images.generate = MagicMock(return_value=_fake_response([_png(), _png(), _png(), _png()]))
        with patch.object(openai_images, "get_client", return_value=client):
            images, _ = await openai_images.call_generate(
                prompt="a cat", size="1024x1024", n=4, quality="low",
            )
        assert len(images) == 4

    @pytest.mark.asyncio
    async def test_count_too_high_rejected(self):
        with pytest.raises(ValueError, match="count/n"):
            await openai_images.call_generate(prompt="x", size="1024x1024", n=99, quality="low")


class TestEditImage:
    @pytest.mark.asyncio
    async def test_single_source(self):
        client = MagicMock()
        client.images.edit = MagicMock(return_value=_fake_response([_png()]))
        with patch.object(openai_images, "get_client", return_value=client):
            images, _ = await openai_images.call_edit(
                prompt="x", images=[(_png(), "image/png")], size="1024x1024", n=1, quality="low",
            )
        assert len(images) == 1

    @pytest.mark.asyncio
    async def test_multi_source_up_to_16(self):
        client = MagicMock()
        client.images.edit = MagicMock(return_value=_fake_response([_png()]))
        with patch.object(openai_images, "get_client", return_value=client):
            srcs = [(_png(), "image/png") for _ in range(16)]
            await openai_images.call_edit(
                prompt="x", images=srcs, size="1024x1024", n=1, quality="low",
            )
        kwargs = client.images.edit.call_args.kwargs
        assert isinstance(kwargs["image"], list) and len(kwargs["image"]) == 16

    @pytest.mark.asyncio
    async def test_too_many_sources_rejected(self):
        with pytest.raises(ValueError, match="At most 16"):
            await openai_images.call_edit(
                prompt="x",
                images=[(_png(), "image/png") for _ in range(17)],
                size="1024x1024", n=1, quality="low",
            )

    @pytest.mark.asyncio
    async def test_aspect_ratio_defaults_to_source_shape(self, monkeypatch):
        """edit_image without explicit aspect_ratio must pick the closest alias
        for the source image's ratio — NOT fall back to 1:1. This is what makes
        the 'edit_image skips intake' convention actually true."""
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["edit_image"].fn

        # 16:9 source → expect 2048x1152 (the 16:9 alias at default tier).
        wide = _png(1600, 900)
        aid = storage.store_asset(wide, "image/png")

        captured = {}
        client = MagicMock()
        def fake_edit(**kwargs):
            captured.update(kwargs)
            return _fake_response([_png()])
        client.images.edit = fake_edit
        monkeypatch.setattr(oi, "get_client", lambda: client)

        await fn(prompt="recolor sky", image=aid)
        assert captured["size"] == "2048x1152", f"expected 16:9 size, got {captured['size']}"

    @pytest.mark.asyncio
    async def test_explicit_aspect_ratio_overrides_source(self, monkeypatch):
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["edit_image"].fn

        wide = _png(1600, 900)
        aid = storage.store_asset(wide, "image/png")

        captured = {}
        client = MagicMock()
        def fake_edit(**kwargs):
            captured.update(kwargs)
            return _fake_response([_png()])
        client.images.edit = fake_edit
        monkeypatch.setattr(oi, "get_client", lambda: client)

        await fn(prompt="recolor sky", image=aid, aspect_ratio="1:1")
        assert captured["size"] == "1024x1024"


# ---------------------------------------------------------------------------
# Cost meta
# ---------------------------------------------------------------------------
class TestCostMeta:
    def test_estimate_low_lt_high(self):
        low = estimate_cost(quality="low", n=1, has_image_input=False, prompt_chars=20)
        high = estimate_cost(quality="high", n=1, has_image_input=False, prompt_chars=20)
        assert low < high

    def test_actual_from_detailed_usage(self):
        usage = {
            "input_tokens_details": {"text_tokens": 100, "image_tokens": 1500},
            "output_tokens_details": {"image_tokens": 1100},
        }
        c = actual_cost_from_usage(usage)
        assert c is not None and c > 0

    def test_actual_handles_missing(self):
        assert actual_cost_from_usage(None) is None
        assert actual_cost_from_usage({}) is None

    def test_attach_meta_keys(self):
        meta = attach_cost_meta(usage={}, quality="low", n=1, has_image_input=False, prompt_chars=10, attempt_count=1)
        for k in ("usage", "estimated_cost_usd", "actual_cost_usd", "attempt_count", "validation_status"):
            assert k in meta


# ---------------------------------------------------------------------------
# SSRF guards on URL fetch (negative paths only — no real network)
# ---------------------------------------------------------------------------
class TestSSRF:
    def test_metadata_blocked(self):
        with pytest.raises(ValueError, match="internal"):
            image_io.fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_blocked(self):
        with pytest.raises(ValueError, match="localhost"):
            image_io.fetch_url("http://localhost:9999/img.png")


# ---------------------------------------------------------------------------
# v0.2 — bearer auth
# ---------------------------------------------------------------------------
class TestBearerAuth:
    def test_open_when_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        ok, reason = check_bearer(None)
        assert ok and reason == "not_required"
        assert not bearer_required()

    def test_missing_when_required(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret123")
        assert bearer_required()
        ok, reason = check_bearer(None)
        assert not ok and reason == "missing"

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret123")
        ok, reason = check_bearer("Bearer wrongtoken")
        assert not ok and reason == "wrong_token"

    def test_bad_scheme_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret123")
        ok, reason = check_bearer("Basic secret123")
        assert not ok and reason == "bad_scheme"

    def test_correct_token_accepted(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret123")
        ok, reason = check_bearer("Bearer secret123")
        assert ok and reason == "ok"


# ---------------------------------------------------------------------------
# v0.2 — cloud storage adapter (mocked S3 / GCS clients)
# ---------------------------------------------------------------------------
class TestCloudStorage:
    @pytest.mark.asyncio
    async def test_returns_none_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(storage, "S3_BUCKET", "")
        monkeypatch.setattr(storage, "GCS_BUCKET", "")
        url = await storage.upload_to_cloud(_png(), "image/png", asset_id="img_x")
        assert url is None

    @pytest.mark.asyncio
    async def test_s3_upload_happy_path(self, monkeypatch):
        monkeypatch.setattr(storage, "S3_BUCKET", "test-bucket")
        monkeypatch.setattr(storage, "S3_REGION", "us-east-1")
        monkeypatch.setattr(storage, "S3_PREFIX", "gpt-image-2")
        captured = {}

        def fake_sync(b, mime, *, asset_id):
            captured.update({"bytes_len": len(b), "mime": mime, "asset_id": asset_id})
            return f"https://test-bucket.s3.us-east-1.amazonaws.com/gpt-image-2/{asset_id}.png"

        monkeypatch.setattr(storage, "_upload_s3_sync", fake_sync)
        url = await storage.upload_to_cloud(_png(), "image/png", asset_id="img_abc")
        assert url and "test-bucket" in url and "img_abc.png" in url
        assert captured["asset_id"] == "img_abc"

    @pytest.mark.asyncio
    async def test_s3_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(storage, "S3_BUCKET", "test-bucket")

        def boom(*a, **kw):
            raise RuntimeError("simulated S3 error")

        monkeypatch.setattr(storage, "_upload_s3_sync", boom)
        url = await storage.upload_to_cloud(_png(), "image/png", asset_id="img_x")
        assert url is None

    @pytest.mark.asyncio
    async def test_gcs_used_if_no_s3(self, monkeypatch):
        monkeypatch.setattr(storage, "S3_BUCKET", "")
        monkeypatch.setattr(storage, "GCS_BUCKET", "gcs-bucket")
        monkeypatch.setattr(storage, "GCS_PREFIX", "gpt-image-2")

        def fake_gcs(b, mime, *, asset_id):
            return f"https://storage.googleapis.com/gcs-bucket/gpt-image-2/{asset_id}.png"

        monkeypatch.setattr(storage, "_upload_gcs_sync", fake_gcs)
        url = await storage.upload_to_cloud(_png(), "image/png", asset_id="img_y")
        assert url and "storage.googleapis.com" in url


# ---------------------------------------------------------------------------
# v0.2 — composite_images registered + multi-source path works
# ---------------------------------------------------------------------------
class TestCompositeImages:
    @pytest.mark.asyncio
    async def test_registered(self):
        # Sanity: server.build_server registers all v0.2 tools.
        import server
        mcp = server.build_server()
        assert "composite_images" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_requires_two_sources(self):
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["composite_images"].fn
        with pytest.raises(ValueError, match="at least 2"):
            await fn(prompt="x", images=[])

    @pytest.mark.asyncio
    async def test_multi_source_calls_edit(self, monkeypatch):
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["composite_images"].fn

        # Pre-populate two assets so resolve_image_input works without HTTP.
        a1 = storage.store_asset(_png(), "image/png")
        a2 = storage.store_asset(_png(), "image/png")

        client = MagicMock()
        client.images.edit = MagicMock(return_value=_fake_response([_png()]))
        monkeypatch.setattr(oi, "get_client", lambda: client)

        result = await fn(prompt="combine", images=[a1, a2], quality="low")
        data = unpack_tool_result(result)
        assert "asset_id" in data
        kwargs = client.images.edit.call_args.kwargs
        assert isinstance(kwargs["image"], list) and len(kwargs["image"]) == 2


# ---------------------------------------------------------------------------
# v0.3 — typography + create_infographic
# ---------------------------------------------------------------------------
class TestTypography:
    def test_render_text_blocks_returns_canvas(self):
        from typography import render_text_blocks
        canvas = PILImage.new("RGBA", (1024, 1024), (255, 255, 255, 255))
        out = render_text_blocks(
            canvas,
            [{"text": "Big Title", "style": "title"},
             {"text": "Subtitle line", "style": "subtitle"},
             {"text": "Body line that may wrap across multiple visible lines", "style": "body"}],
            x=40, y=40, max_width=900, align="left",
        )
        assert out.size == (1024, 1024)

    def test_measure_blocks_grows_with_lines(self):
        from typography import measure_blocks
        _, h_short = measure_blocks(
            [{"text": "short", "style": "body"}],
            max_width=600, canvas_min_edge=1024,
        )
        long_text = "word " * 200
        _, h_long = measure_blocks(
            [{"text": long_text, "style": "body"}],
            max_width=600, canvas_min_edge=1024,
        )
        assert h_long > h_short

    def test_composite_typography_block_returns_png(self):
        from typography import composite_typography_block
        base = _png(800, 800, color=(240, 240, 240, 255))
        out = composite_typography_block(
            base,
            [{"text": "Title", "style": "title"},
             {"text": "Body text here.", "style": "body"}],
            bbox={"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.5},
            align="left",
        )
        assert out[:8] == b"\x89PNG\r\n\x1a\n"


class TestCreateInfographic:
    @pytest.mark.asyncio
    async def test_registered(self):
        import server
        mcp = server.build_server()
        assert "create_infographic" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_unknown_layout_rejected(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["create_infographic"].fn
        with pytest.raises(ValueError, match="Unknown layout"):
            await fn(background_prompt="x", layout="hexagon", content={"title": "y"})

    @pytest.mark.asyncio
    async def test_text_rendered_deterministically(self, monkeypatch):
        """Verify the AI is called for the BACKGROUND only — no text passed to the model."""
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["create_infographic"].fn

        captured = {}
        client = MagicMock()
        def fake_generate(**kwargs):
            captured.update(kwargs)
            return _fake_response([_png(1024, 1024)])
        client.images.generate = fake_generate
        monkeypatch.setattr(oi, "get_client", lambda: client)

        result = await fn(
            background_prompt="A calm peach gradient",
            layout="square-list",
            content={
                "title": "Spring Sale",
                "bullets": ["proven.com/spring", "30% off Q1 favorites"],
                "footer": "Ends April 30, 2026",
            },
            quality="low",
        )

        # Background prompt must NOT include any of the text content (model never sees it).
        bg_prompt = captured.get("prompt", "")
        for forbidden in ("Spring Sale", "proven.com/spring", "30% off", "April 30"):
            assert forbidden not in bg_prompt, f"text leaked to AI prompt: {forbidden!r}"
        # Output is a real PNG asset.
        data = unpack_tool_result(result)
        assert "asset_id" in data and data["mime"] == "image/png"

    @pytest.mark.asyncio
    async def test_layouts_listed(self):
        from infographic import list_layouts
        layouts = list_layouts()
        assert "portrait-stat" in layouts
        assert "landscape-comparison" in layouts
        assert "square-list" in layouts


# ---------------------------------------------------------------------------
# v0.4 — refine_image (Responses API, forced tool_choice)
# ---------------------------------------------------------------------------
def _fake_responses_response(images: list[bytes], response_id: str = "resp_xyz") -> MagicMock:
    """Build a fake Responses-API response with image_generation_call items."""
    resp = MagicMock()
    resp.id = response_id
    items = []
    for b in images:
        item = MagicMock()
        item.type = "image_generation_call"
        item.id = "img_call_abc"
        item.result = base64.b64encode(b).decode()
        items.append(item)
    resp.output = items
    resp.usage = {
        "input_tokens": 200,
        "output_tokens": 1100,
        "input_tokens_details": {"text_tokens": 200, "image_tokens": 1500},
        "output_tokens_details": {"image_tokens": 1100},
    }
    return resp


class TestRefineImage:
    @pytest.mark.asyncio
    async def test_registered(self):
        import server
        mcp = server.build_server()
        assert "refine_image" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_first_turn_requires_image(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn
        with pytest.raises(ValueError, match="first turn|First turn"):
            await fn(prompt="darken")

    @pytest.mark.asyncio
    async def test_chain_form_mutually_exclusive(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn
        with pytest.raises(ValueError, match="at most one"):
            await fn(prompt="x", previous_response_id="r1", image_generation_call_id="c1")

    @pytest.mark.asyncio
    async def test_first_turn_calls_responses_with_forced_tool_choice(self, monkeypatch):
        import server, openai_responses as orsp
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn

        # Pre-store an asset to use as input.
        aid = storage.store_asset(_png(64, 64), "image/png")

        captured = {}
        client = MagicMock()
        def fake_create(**kwargs):
            captured.update(kwargs)
            return _fake_responses_response([_png()], response_id="resp_first")
        client.responses.create = fake_create
        monkeypatch.setattr(orsp, "get_client", lambda: client)

        result = await fn(prompt="darken background", image=aid)

        assert captured["tool_choice"] == {"type": "image_generation"}
        assert captured["tools"] == [{"type": "image_generation"}]
        # First turn includes the image as an input_image content block.
        content = captured["input"][0]["content"]
        assert any(c.get("type") == "input_image" for c in content)
        # Output captured the response_id for chaining.
        assert unpack_tool_result(result)["response_id"] == "resp_first"

    @pytest.mark.asyncio
    async def test_chained_turn_uses_previous_response_id(self, monkeypatch):
        import server, openai_responses as orsp
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn

        captured = {}
        client = MagicMock()
        def fake_create(**kwargs):
            captured.update(kwargs)
            return _fake_responses_response([_png()], response_id="resp_second")
        client.responses.create = fake_create
        monkeypatch.setattr(orsp, "get_client", lambda: client)

        result = await fn(prompt="now lighten", previous_response_id="resp_first")
        assert captured["previous_response_id"] == "resp_first"
        # No image_input on chain turns.
        content = captured["input"][0]["content"]
        assert not any(c.get("type") == "input_image" for c in content)
        assert unpack_tool_result(result)["response_id"] == "resp_second"

    @pytest.mark.asyncio
    async def test_image_generation_call_id_chain(self, monkeypatch):
        import server, openai_responses as orsp
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn

        captured = {}
        client = MagicMock()
        def fake_create(**kwargs):
            captured.update(kwargs)
            return _fake_responses_response([_png()])
        client.responses.create = fake_create
        monkeypatch.setattr(orsp, "get_client", lambda: client)

        await fn(prompt="x", image_generation_call_id="img_call_abc")
        content = captured["input"][0]["content"]
        gen_refs = [c for c in content if c.get("type") == "image_generation_call"]
        assert len(gen_refs) == 1 and gen_refs[0]["id"] == "img_call_abc"

    @pytest.mark.asyncio
    async def test_reasoning_effort_passed(self, monkeypatch):
        import server, openai_responses as orsp
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["refine_image"].fn

        aid = storage.store_asset(_png(), "image/png")
        captured = {}
        client = MagicMock()
        def fake_create(**kwargs):
            captured.update(kwargs)
            return _fake_responses_response([_png()])
        client.responses.create = fake_create
        monkeypatch.setattr(orsp, "get_client", lambda: client)

        await fn(prompt="x", image=aid, reasoning_effort="medium")
        assert captured["reasoning"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# v1.0 — Studio UI: /app HTML page and /api/{tool} JSON shim
# ---------------------------------------------------------------------------
class TestStudioUI:
    def _client(self):
        from starlette.testclient import TestClient
        import server
        mcp = server.build_server()
        return TestClient(mcp.streamable_http_app()), mcp

    def test_app_page_served(self):
        client, _ = self._client()
        r = client.get("/app")
        assert r.status_code == 200
        assert "gpt-image-2 Studio" in r.text
        # All 8 tools surfaced in the JS config.
        for tool in ("upload_image", "generate_image", "edit_image", "add_text_overlay",
                     "add_link_cta", "composite_images", "create_infographic", "refine_image"):
            assert tool in r.text

    def test_api_unknown_tool_404(self):
        client, _ = self._client()
        r = client.post("/api/does_not_exist", json={})
        assert r.status_code == 404

    def test_api_non_json_body_400(self):
        client, _ = self._client()
        r = client.post("/api/add_text_overlay", data="not-json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_api_routes_to_tool_fn(self):
        client, _ = self._client()
        # Pre-store a source asset so add_text_overlay (deterministic, no API) can run.
        aid = storage.store_asset(_png(256, 256), "image/png")
        r = client.post("/api/add_text_overlay", json={
            "image": aid, "text": "Spring Sale", "position": "bottom-center",
            "style": "headline", "color": "#ffffff",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["asset_id"].startswith("img_")
        assert body["validation_status"] == "ok"

    def test_api_value_error_returns_400(self):
        client, _ = self._client()
        # refine_image first turn requires an image — should raise ValueError → 400.
        r = client.post("/api/refine_image", json={"prompt": "darken"})
        assert r.status_code == 400
        assert "first turn" in r.json()["error"].lower()

    def test_api_bearer_required_when_set(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "studio-secret")
        client, _ = self._client()
        r = client.post("/api/add_text_overlay", json={"image": "img_x", "text": "hi"})
        assert r.status_code == 401
        assert "Unauthorized" in r.json()["error"]


# ---------------------------------------------------------------------------
# v1.0 — Rate limit (single-instance safety net)
# ---------------------------------------------------------------------------
class TestRateLimit:
    def setup_method(self):
        from security import reset_rate_limits
        reset_rate_limits()

    def test_unset_is_unlimited(self, monkeypatch):
        from security import check_rate_limit
        monkeypatch.delenv("RATE_LIMIT_PER_MINUTE", raising=False)
        for _ in range(50):
            ok, _ = check_rate_limit("1.2.3.4")
            assert ok

    def test_zero_is_unlimited(self, monkeypatch):
        from security import check_rate_limit
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
        for _ in range(50):
            ok, _ = check_rate_limit("1.2.3.4")
            assert ok

    def test_caps_at_configured_value(self, monkeypatch):
        from security import check_rate_limit
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
        ip = "9.9.9.9"
        assert check_rate_limit(ip)[0] is True
        assert check_rate_limit(ip)[0] is True
        assert check_rate_limit(ip)[0] is True
        ok, retry = check_rate_limit(ip)
        assert ok is False and retry > 0

    def test_per_ip_buckets_independent(self, monkeypatch):
        from security import check_rate_limit
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
        assert check_rate_limit("a")[0] is True
        assert check_rate_limit("a")[0] is False
        # Different IP gets its own quota.
        assert check_rate_limit("b")[0] is True

    def test_xff_header_used_when_present(self):
        from security import client_ip_from_request

        class Req:
            headers = {"x-forwarded-for": "203.0.113.5, 10.0.0.1"}
            client = type("C", (), {"host": "10.0.0.1"})()

        assert client_ip_from_request(Req()) == "203.0.113.5"


class TestStudioConsistency:
    """Studio UI must stay in sync with the underlying tool surface."""

    def _html(self):
        from starlette.testclient import TestClient
        import server
        mcp = server.build_server()
        return TestClient(mcp.streamable_http_app()).get("/app").text

    def test_aspect_ratio_options_consistent(self):
        html = self._html()
        # All three image-generating tools must offer the full ASPECTS_FULL set.
        # Single source of truth in JS — assert it lists every alias from the shim.
        for alias in ("1:1", "4:5", "5:4", "16:9", "9:16", "3:2", "2:3",
                      "social-square", "social-vertical", "social-horizontal",
                      "infographic-portrait", "infographic-landscape"):
            assert alias in html, f"aspect alias {alias!r} missing from Studio JS"

    def test_composite_exposes_count_and_output_format(self):
        html = self._html()
        # Pull the composite_images tool block and confirm it exposes both fields.
        i = html.index("id:'composite_images'")
        block = html[i:i + 800]
        assert "k:'count'" in block
        assert "k:'output_format'" in block

    def test_infographic_default_swap(self):
        html = self._html()
        # Each layout has its own default content schema swapped on change.
        assert "INFOGRAPHIC_DEFAULTS" in html
        for layout in ("square-list", "portrait-stat", "landscape-comparison"):
            assert layout in html

    def test_bearer_header_sent_on_fetch(self):
        html = self._html()
        # Both /upload and /api/{tool} fetches go through authHeaders().
        assert "authHeaders(" in html
        # And the bearer input is wired to localStorage.
        assert "gptimg2.bearer" in html


class TestIntakeContract:
    """Lock in the intake-required pattern across server + tool docstrings.

    If these fail, an agent might call generate_image/composite_images/create_infographic
    without confirming aspect_ratio + quality with the user — losing the ability to
    catch mismatches before paying for the call.
    """
    def test_server_instructions_mention_intake(self):
        import server
        mcp = server.build_server()
        instr = (mcp.instructions or "").lower()
        assert "intake" in instr
        assert "aspect_ratio" in instr
        assert "quality" in instr

    def test_each_image_generating_tool_has_intake_required(self):
        import server
        mcp = server.build_server()
        for name in ("generate_image", "composite_images", "create_infographic"):
            tool = mcp._tool_manager._tools[name]
            desc = (tool.description or "").lower()
            assert "intake required" in desc, f"{name} missing INTAKE REQUIRED in description"

    def test_source_shaped_tools_skip_intake(self):
        import server
        mcp = server.build_server()
        # These default to source-image shape — agents shouldn't be forced to ask.
        for name in ("edit_image", "refine_image", "add_text_overlay", "add_link_cta", "upload_image"):
            tool = mcp._tool_manager._tools[name]
            desc = (tool.description or "").lower()
            assert "intake required" not in desc, f"{name} unexpectedly has INTAKE REQUIRED"


class TestRateLimitEnforcement:
    def test_429_on_upload_when_exceeded(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
        from starlette.testclient import TestClient
        import server
        mcp = server.build_server()
        client = TestClient(mcp.streamable_http_app())
        # First request: 201/400 depending on body — both count toward the limit.
        client.post("/upload", content=b"")  # 400 expected (empty body)
        # Second request from same IP: 429.
        r = client.post("/upload", content=b"")
        assert r.status_code == 429
        assert r.headers.get("retry-after")


# ---------------------------------------------------------------------------
# Native Claude rendering — [Image, ..., render_md_str, json_str]
# ---------------------------------------------------------------------------
class TestNativeRenderingShape:
    """Image-output tools must return FastMCP block lists, not raw dicts.

    Tool pane gets inline thumbnails (Image), chat gets render_md, JSON tail
    is for tool chaining and the /api/{tool} shim.
    """

    @pytest.mark.asyncio
    async def test_deterministic_tool_returns_blocks(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["add_text_overlay"].fn
        aid = storage.store_asset(_png(256, 256), "image/png")
        result = await fn(image=aid, text="Spring", style="headline")

        from mcp.server.fastmcp import Image
        assert isinstance(result, list)
        # Last two blocks are the markdown and the JSON.
        assert isinstance(result[-1], str) and result[-1].startswith("{")
        assert isinstance(result[-2], str) and "![](" in result[-2]
        # Everything before is an Image thumbnail block.
        thumbs = result[:-2]
        assert thumbs and all(isinstance(b, Image) for b in thumbs)

    @pytest.mark.asyncio
    async def test_render_md_has_download_and_save_nudge(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["add_text_overlay"].fn
        aid = storage.store_asset(_png(128, 128), "image/png")
        result = await fn(image=aid, text="Hi", style="headline")
        render_md = result[-2]
        assert "[Download image]" in render_md
        assert "Save to Google Drive?" in render_md
        # Extract URL and confirm the markdown embed wraps it.
        data = unpack_tool_result(result)
        assert f"![]({data['image_url']})" in render_md

    @pytest.mark.asyncio
    async def test_multi_image_response_lists_each(self, monkeypatch):
        import server, openai_images as oi
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["composite_images"].fn
        a1 = storage.store_asset(_png(), "image/png")
        a2 = storage.store_asset(_png(), "image/png")
        client = MagicMock()
        client.images.edit = MagicMock(return_value=_fake_response([_png(), _png(), _png()]))
        monkeypatch.setattr(oi, "get_client", lambda: client)

        result = await fn(prompt="combine", images=[a1, a2], quality="low", count=3)
        from mcp.server.fastmcp import Image
        thumbs = [b for b in result if isinstance(b, Image)]
        assert len(thumbs) == 3
        render_md = result[-2]
        # One embed + download per image, plus the trailing nudge.
        assert render_md.count("[Download image") == 3

    @pytest.mark.asyncio
    async def test_upload_image_skips_save_nudge(self):
        # upload_image is utility, not a creative output — no Save-to-Drive nudge.
        import server
        from io import BytesIO as _BIO
        mcp = server.build_server()
        # Patch fetch_url to return our fixture bytes (simulates URL fetch).
        import tools as tools_mod
        png = _png()
        original = tools_mod.fetch_url
        tools_mod.fetch_url = lambda url: (png, "image/png")
        try:
            fn = mcp._tool_manager._tools["upload_image"].fn
            result = await fn(image="https://example.com/x.png")
        finally:
            tools_mod.fetch_url = original
        render_md = result[-2]
        assert "Save to Google Drive?" not in render_md
        assert "![](" in render_md

    @pytest.mark.asyncio
    async def test_unpack_tool_result_works_for_dict_and_list(self):
        # Backward-compat: bare dicts pass through unchanged.
        assert unpack_tool_result({"asset_id": "x"}) == {"asset_id": "x"}
        # List shape: extract the JSON tail.
        import json as _json
        result = ["thumbnail-placeholder", "render_md", _json.dumps({"asset_id": "y"})]
        assert unpack_tool_result(result) == {"asset_id": "y"}


class TestUploadHelpMessage:
    """upload_image should return environment-aware fix-it snippets on bad input.

    Claude.ai web / Cowork uses the Python urllib snippet; Claude Code uses curl.
    Both must always be present so the agent picks the right one.
    """
    def test_data_uri_rejection_lists_both_paths(self):
        from tools import _upload_help_message
        msg = _upload_help_message("Base64 / data: URIs are NOT accepted as MCP parameters.")
        assert "claude.ai web OR Claude Cowork" in msg
        assert "Claude Code (CLI)" in msg
        assert "Manual upload" in msg
        # Snippet auto-discovery covers both /mnt and Cowork session dirs.
        assert "/mnt/user-data/uploads" in msg
        assert "local-agent-mode-sessions" in msg
        # Curl path is for Claude Code Bash tool.
        assert "curl -fsS -X POST" in msg

    def test_invalid_path_substitutes_into_curl_snippet(self):
        from tools import _claude_code_snippet
        cmd = _claude_code_snippet("/Users/me/photo.jpg")
        # User-provided path goes verbatim into --data-binary so the agent can run it.
        assert '"@/Users/me/photo.jpg"' in cmd

    @pytest.mark.asyncio
    async def test_upload_image_data_uri_raises_with_help(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["upload_image"].fn
        with pytest.raises(ValueError) as ei:
            await fn(image="data:image/png;base64,iVBORw0KGgo")
        msg = str(ei.value)
        assert "claude.ai web" in msg
        assert "Claude Code" in msg

    @pytest.mark.asyncio
    async def test_upload_image_missing_file_raises_with_help(self):
        import server
        mcp = server.build_server()
        fn = mcp._tool_manager._tools["upload_image"].fn
        with pytest.raises(ValueError) as ei:
            await fn(image="/no/such/file.png")
        msg = str(ei.value)
        assert "claude.ai web" in msg
        assert '"@/no/such/file.png"' in msg  # path threaded into curl snippet

    def test_bearer_token_threaded_into_snippets(self, monkeypatch):
        from tools import _urllib_snippet, _claude_code_snippet
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-xyz")
        py = _urllib_snippet()
        assert "Authorization" in py
        assert "MCP_BEARER_TOKEN" in py
        bash = _claude_code_snippet()
        assert "Authorization: Bearer $MCP_BEARER_TOKEN" in bash


class TestServerInstructions:
    """The instructions block carries critical agent-facing UX guidance."""

    def test_environment_routing_blocks_present(self):
        import server
        mcp = server.build_server()
        instr = mcp.instructions or ""
        assert "claude.ai web" in instr
        assert "Claude Cowork" in instr
        assert "Claude Code" in instr
        assert "/mnt/user-data/uploads" in instr
        assert "local-agent-mode-sessions" in instr
        # Save-to-Drive workflow taught in the instructions.
        assert "Save to Google Drive" in instr

    def test_presentation_rules_documented(self):
        import server
        mcp = server.build_server()
        instr = (mcp.instructions or "").lower()
        # Agents must surface markdown embed + download link from render_md.
        assert "image embed" in instr or "![](" in mcp.instructions
        assert "download" in instr
        assert "json metadata" in instr or "tool chaining" in instr
