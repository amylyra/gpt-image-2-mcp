# gpt-image-2-mcp — Troubleshooting

## Server won't start

> `ERROR: Required env vars missing: IMAGE_URL_SIGNING_KEY`

Generate one and put it in `.env`:
```bash
echo "IMAGE_URL_SIGNING_KEY=$(openssl rand -hex 32)" >> .env
```

> `OPENAI_API_KEY env var is required`

Set it. The SDK is constructed lazily, so the error fires on the first API call rather than at import.

## "Asset not found" on every tool that takes a URL

The `image_url` returned by an earlier tool expired (default 24h) or the server was restarted (in-memory store). Re-upload the image, or pass an `asset_id` from a freshly returned response.

In v0.1, restart = lose all stored assets. Wait for v0.2 (S3/GCS) for durability.

## `add_text_overlay` URL has wrong characters

You're using `render_mode="ai_integrated"`. Switch to `render_mode="deterministic"` (the default). The deterministic path uses Pillow and renders text exactly.

If you must use `ai_integrated` (e.g., text on a sign in the scene), do not put URLs through it.

## `400 invalid_request_error` from OpenAI

Most common causes:

- You're somehow bypassing the adapter and passing `response_format` / `thinking` / `input_fidelity` / `background="transparent"` directly. Don't.
- Size doesn't satisfy the constraints (max edge 3840, multiples of 16, ratio ≤3:1, pixels in [655_360, 8_294_400]). Use `size_for_aspect()` — never construct sizes manually.
- Mask dimensions don't match the base image. `validate_mask` rejects this pre-call; if you got here, you bypassed the wrapper.

## `429 rate_limit_exceeded`

The SDK surfaces rate-limit headers in the exception. Wait a few seconds and retry. If it's persistent, lower `MAX_COUNT_PER_REQUEST` (default 4) or stagger calls.

## Cloud Run image URLs return localhost

Set `PUBLIC_URL` to the Cloud Run service URL (with no trailing slash):
```bash
gcloud run services update gpt-image-2-mcp \
  --set-env-vars PUBLIC_URL=https://your-service-hash.region.run.app
```

`storage._compute_base_url()` reads `PUBLIC_URL` once at import. After changing it, redeploy.

## "Bad sig" 404 on a URL that looks correct

- The signing key has rotated. URLs signed with the old key are permanently invalid. Re-upload to mint a new URL.
- The URL was edited (truncated, query string mangled by a chat client). Copy it whole.

## `validate_image_params` rejects something the user explicitly asked for

That's the design — gpt-image-2 doesn't accept `response_format`, `thinking`, `input_fidelity`, or `background="transparent"`. Either rewrite the request without those params, or use a different model/server (Nano Banana for transparency).

## Tests fail with `ImportError: dotenv` (or similar)

`requirements.txt` doesn't pin `python-dotenv` because it's optional. The server imports it inside a `try/except`. If you want `.env` auto-loading: `pip install python-dotenv`.

## "Base64 / data: URIs are not accepted as MCP parameters"

Don't paste raw base64 or `data:` URIs into tool params — MCP transport hangs before the server can reject them. Either:
1. POST the bytes to `/upload` (HTTP mode) and pass the returned `asset_id`.
2. In stdio mode, `upload_image(image="/full/path/to/file.jpg")` works.

## Smoke test fails with `cost ${cost} exceeded budget $0.10`

The `--cheap` path uses `quality="low"` and small sizes; if your account is being billed at higher rates or the prompt was long, raise the budget:
```bash
python smoke_openai_contract.py --cheap --budget 0.20
```
