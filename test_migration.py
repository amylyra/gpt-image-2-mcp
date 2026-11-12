"""Offline migration checks. No OpenAI requests, brand renders, or cloud writes.

Synthetic images exercise plumbing only; they do not certify visual quality or
execution of either Claude plugin's Skill pipeline.
"""
import base64
import json
import os
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI, BadRequestError
from PIL import Image

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("IMAGE_URL_SIGNING_KEY", "0" * 64)
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import openai_images as adapter
import server
import storage
from tools import unpack_tool_result

MODELS = ["gpt-image-2.6-sunburst", "gpt-image-2.6-flare"]


def png(index=0, size=(256, 256)):
    out = BytesIO()
    Image.new("RGBA", size, (index * 20, 80, 120, 255)).save(out, "PNG")
    return out.getvalue()


def response(count=1):
    return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png()).decode())
                                 for _ in range(count)], usage={"output_tokens": 1})


@pytest.mark.parametrize("configured,expected", [(None, MODELS[0]), (MODELS[1], MODELS[1]),
                                               ("gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst")])
def test_existing_environment_override_wins(configured, expected):
    env = dict(os.environ)
    env.pop("OPENAI_IMAGE_MODEL", None)
    if configured:
        env["OPENAI_IMAGE_MODEL"] = configured
    result = subprocess.check_output([sys.executable, "-c",
                                      "import openai_images; print(openai_images.DEFAULT_MODEL)"],
                                     env=env, text=True).strip()
    assert result == expected


@pytest.mark.asyncio
async def test_live_mcp_input_contract_preserved():
    captured = json.loads(Path("fixtures/mcp-tools-2026-11-12.json").read_text())
    live = {t["name"]: t["inputSchema"] for t in captured["result"]["tools"]}
    current = {t.name: t.inputSchema for t in await server.build_server().list_tools()}
    assert current == live


# Order is intentional: base/identity before layout, tint, or geometry references.
CASES = [
    ("proven_skin", 1, "3:4", "medium", 3),
    ("proven_glass_overlay", 3, "3:4", "medium", 1),
    ("proven_text_edit", 1, "4:5", "medium", 1),
    ("nw_notes_composite", 4, "4:5", "medium", 1),
    ("nw_product_scene", 2, "4:5", "medium", 1),
    ("nw_sku_pdp", 1, "1:1", "high", 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("case,ref_count,ratio,quality,count", CASES)
async def test_brand_edit_plumbing(monkeypatch, model, case, ref_count, ratio, quality, count):
    monkeypatch.setattr(storage, "S3_BUCKET", "")
    monkeypatch.setattr(storage, "GCS_BUCKET", "")
    monkeypatch.setattr(adapter, "DEFAULT_MODEL", model)
    inputs = [png(i) for i in range(ref_count)]
    ids = [storage.store_asset(b, "image/png") for b in inputs]
    client = MagicMock()
    client.images.edit.return_value = response(count)
    monkeypatch.setattr(adapter, "get_client", lambda: client)
    prompt = case + ': preserve identity and reference order; exact text "PROVEN 24h".'
    fn = server.build_server()._tool_manager._tools["edit_image"].fn
    result = await fn(prompt=prompt, image=ids, aspect_ratio=ratio, quality=quality, count=count)
    kwargs = client.images.edit.call_args.kwargs
    assert kwargs["model"] == model and kwargs["prompt"] == prompt
    assert kwargs["quality"] == quality and kwargs["n"] == count
    assert kwargs["size"] == adapter.size_for_aspect(ratio, prefer_high=quality == "high")
    files = kwargs["image"] if isinstance(kwargs["image"], list) else [kwargs["image"]]
    assert [f.getvalue() for f in files] == inputs
    meta = unpack_tool_result(result)
    items = meta.get("images", [meta])
    assert len(items) == count
    for item in items:
        assert item["asset_id"].startswith("img_")
        assert item["image_url"].startswith("http://localhost:8080/images/")
        assert item["mime"] == "image/png"
        assert storage.resolve_asset(item["asset_id"])[0] == png()
    assert "Download image" in result[-2]
    client.images.edit.assert_called_once()


def multipart(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.read())
    parts = list(message.iter_parts())
    fields = {p.get_param("name", header="content-disposition"): p.get_content().strip()
              for p in parts if not p.get_filename()}
    files = [p for p in parts if p.get_filename()]
    return fields, files


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_sdk_serializes_generate_edit_and_mask(monkeypatch, model):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"created": 1, "data": [
            {"b64_json": base64.b64encode(png()).decode()}], "usage": {"output_tokens": 1}})
    with OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        monkeypatch.setattr(adapter, "get_client", lambda: client)
        await adapter.call_generate(model=model, prompt="text", size="1024x1024", quality="medium")
        await adapter.call_edit(model=model, prompt="edit", images=[(png(i), "image/png") for i in range(3)],
                                mask=png(), size="1152x1536", quality="medium")
    assert requests[0].url.path == "/v1/images/generations"
    assert json.loads(requests[0].content)["model"] == model
    fields, files = multipart(requests[1])
    assert requests[1].url.path == "/v1/images/edits"
    assert fields["model"] == model and fields["size"] == "1152x1536"
    assert [p.get_payload(decode=True) for p in files[:-1]] == [png(i) for i in range(3)]
    assert files[-1].get_filename() == "mask.png"
    assert not ({"response_format", "thinking", "input_fidelity", "complexity"} & fields.keys())


@pytest.mark.asyncio
async def test_upstream_rejection_propagates_without_cross_model_retry(monkeypatch):
    attempts = []
    def reject(request):
        attempts.append(request)
        return httpx.Response(400, json={"error": {"message": "unsupported size", "type": "invalid_request_error"}})
    with OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(reject))) as client:
        monkeypatch.setattr(adapter, "get_client", lambda: client)
        with pytest.raises(BadRequestError):
            await adapter.call_generate(model=MODELS[0], prompt="x", size="1024x1024")
    assert len(attempts) == 1


@pytest.mark.parametrize("high", [False, True])
def test_skin_three_four_alias_valid(high):
    width, height = adapter.parse_size(adapter.size_for_aspect("3:4", prefer_high=high))
    adapter.validate_size(width, height)
    assert width / height == 3 / 4
