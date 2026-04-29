# gpt-image-2-mcp

MCP server for OpenAI's **gpt-image-2** (released 2026-04-21), focused on **post-edit work**: text overlays, link/CTA additions, brand-consistent infographics, identity-preserving edits.

Companion to [`nanobanana-mcp`](../nanobanana-mcp). Both servers coexist — route by tool description: gpt-image-2 for text-heavy / infographic / multi-reference / identity-preserving work, Nano Banana for raw artistic / photoreal generation.

---

## Tool surface

| Tool | What it does | Default mode | Phase |
|---|---|---|---|
| `upload_image`      | URL or local path → `asset_id` + signed `image_url`. | n/a | v0.1 |
| `generate_image`    | Text-to-image. Variants via `count` (1–4). | gpt-image-2 | v0.1 |
| `edit_image`        | Edit 1–16 source images, optional mask. | gpt-image-2 | v0.1 |
| `add_text_overlay`  | Text overlay (headline / badge / caption / watermark). | **deterministic PIL** (zero API cost, exact text) | v0.1 |
| `add_link_cta`      | CTA + URL (button / bar / pill / underlined-link). | **deterministic PIL** (URL rendered character-for-character) | v0.1 |
| `composite_images`  | Combine 2–16 sources into one new image (product-on-bg, character-into-scene). | gpt-image-2 | v0.2 |
| `create_infographic`| Hybrid: gpt-image-2 generates BACKGROUND only; deterministic text composited on top. URLs / stats are 100% accurate (model never sees them). | hybrid | v0.3 |
| `refine_image`      | Multi-turn iterative refinement via Responses API with forced `tool_choice`. Chain via `previous_response_id` or `image_generation_call_id`. | gpt-5.5 | v0.4 |

Every response includes `asset_id`, signed `image_url`, `expires_at`, `width`, `height`, `size_kb`, plus a cost block (`usage`, `estimated_cost_usd`, `actual_cost_usd`, `attempt_count`, `validation_status`).

All tools accept either an http(s) URL **or** an `asset_id` for image inputs — multi-turn edits work after URLs expire.

## Response shape — native Claude rendering

Image-output tools return a 3-part FastMCP block list, the same pattern Nano Banana uses:

```
[Image(thumb_jpeg), ..., render_md_str, json_str]
```

- `Image(...)` — 1024px JPEG thumbnails. Render inline in the tool pane and the chat reply across Claude Code, Claude Cowork, and claude.ai web.
- `render_md_str` — markdown image embeds + `[Download image](url)` links + a trailing **Save to Google Drive?** nudge. The agent should surface this verbatim so users always get a reliable download link.
- `json_str` — full metadata (`asset_id`, `image_url`, `usage`, costs, `response_id`, etc.) for tool chaining and the `/api/{tool}` shim. The shim auto-extracts this for clean REST output.

When the user replies *save* / *save to drive*, the agent fetches bytes from the most recent `image_url` and calls Google Drive MCP's `create_file` — taught in `server.instructions`, `CLAUDE.md`, and the companion skill.

## Intake before image-generating tools

`generate_image`, `composite_images`, and `create_infographic` produce a NEW image whose shape is the user's call. Before the agent calls any of these, it must confirm `aspect_ratio` (or `layout` for infographic) and `quality` with the user — unless they've already named both. The convention is enforced at two levels (server `instructions` + each tool's description) so both well-behaved and bare-bones MCP clients see it.

`edit_image`, `refine_image`, `add_text_overlay`, and `add_link_cta` skip intake — they default to the source image's shape (`edit_image` picks the closest aspect alias automatically; pass `aspect_ratio` only to override).

## Why deterministic by default

gpt-image-2 is the strongest text-rendering image model available, but still imperfect. For URL/CTA copy where `proven.com/spring` MUST NOT become `proven.com/sprlng`, the deterministic Pillow path renders text exactly and costs $0 per call. Use `render_mode="ai_integrated"` only when you need the text physically integrated into the scene (sign in the image, label on a product, etc.) and exact fidelity isn't required.

For full infographics, `create_infographic` extends the same idea: gpt-image-2 generates the background (no text), and the title / bullets / stat / footer are rendered via Pillow afterwards. The model literally never sees the user's text content — exact fidelity is structurally guaranteed.

## Infographic content schemas

```python
# portrait-stat — big stat with label
content = {"stat": "47%", "subtitle": "of users prefer X", "footer": "Q1 2026, n=1,200"}

# landscape-comparison — title + two columns
content = {
  "title": "Before vs After",
  "left":  ["• Manual exports", "• 4 hour cycle", "• 12 % error rate"],
  "right": ["• Automated", "• 8 minute cycle", "• <1 % error rate"],
}

# square-list — title + bullets
content = {
  "title": "Spring Sale",
  "subtitle": "Limited-time savings on Q1 favorites",
  "bullets": ["30% off Cleansers", "20% off Serums", "Free shipping over $50"],
  "footer": "proven.com/spring",
}
```

---

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Required: OPENAI_API_KEY and IMAGE_URL_SIGNING_KEY
# Generate key: openssl rand -hex 32

python server.py
# → http://localhost:8080
# Boot fails fast if either required env var is missing.
```

Open `http://localhost:8080/upload` for a drag-drop form (returns `{asset_id, image_url, …}`), or `http://localhost:8080/app` for the **Studio UI** — tool tabs for all 8 tools, drag-drop input, output preview, asset_id history (localStorage). Studio drives the tools through `/api/{tool}`, a JSON shim that honors `MCP_BEARER_TOKEN`.

## Upload paths — by environment

The MCP can run remote (Cloud Run) or local (stdio). Files only exist where the user is, so the upload path depends on the client. `upload_image` returns an environment-aware fix-it message when fed a data URI or unreachable path so the agent picks the right one:

| Environment | Tool | Path |
|---|---|---|
| claude.ai web | Python | urllib snippet → `/upload` (auto-discovers `/mnt/user-data/uploads`) |
| Claude Cowork (Mac) | Python | same urllib snippet (auto-discovers `~/Library/Application Support/Claude/local-agent-mode-sessions/<session>/uploads`) |
| Claude Code (CLI) — remote MCP | Bash | `curl --data-binary "@/path"` → `/upload` |
| Claude Code — local stdio MCP | n/a | `upload_image(image='/path')` reads the file server-side |
| Any | n/a | drag-drop at `{PUBLIC_URL}/upload` or full `{PUBLIC_URL}/app` Studio UI |

Both snippets thread `MCP_BEARER_TOKEN` automatically when set.

## Stdio mode

```bash
MCP_TRANSPORT=stdio python server.py
```

In stdio mode, `upload_image` also accepts local file paths. (HTTP mode rejects them — files don't exist on the server.)

## Cloud Run

```bash
gcloud run deploy gpt-image-2-mcp \
  --source . \
  --region us-central1 \
  --min-instances=1 \
  --set-env-vars OPENAI_API_KEY=...,IMAGE_URL_SIGNING_KEY=...,PUBLIC_URL=https://your.run.app
```

Set `PUBLIC_URL` — without it, signed image URLs use localhost and break in production.

---

## What's NOT supported (gpt-image-2 contract)

The adapter rejects these before the SDK call:

- `response_format` — gpt-image-2 always returns base64.
- `thinking` — not an Image API parameter.
- `input_fidelity` — not an Image API parameter.
- `background="transparent"` — not supported.
- `output_compression` with `output_format="png"` — only valid for jpeg/webp.

If a tool ever tries to pass any of these, you get a `ValueError` before a single token is spent.

---

## Aspect ratios

The shim preserves true ratios across `default` and `high` (4K) tiers.

```
1:1                 1024x1024     2048x2048
4:5                 1024x1280     1536x1920
16:9                2048x1152     3840x2160
9:16                1152x2048     2160x3840
3:2                 1536x1024     3072x2048
2:3                 1024x1536     2048x3072
infographic-portrait    1024x1536     2048x3072
infographic-landscape   1536x1024     3072x2048
social-square           1024x1024     2048x2048
social-vertical         1152x2048     2160x3840
social-horizontal       2048x1152     3840x2160
```

`prefer_high` is set automatically when `quality="high"`. Tools never construct size strings directly — `size_for_aspect()` is the only entry point.

---

## Tests

```bash
python -m pytest test_simulation.py -x -q
# 53 mocked tests, no API key needed.

python smoke_openai_contract.py --cheap
# 1 generate + 1 edit, quality=low. Costs ~$0.02–$0.05.
```

The contract test asserts the SDK is never called with banned params.

---

## Storage

By default the server runs with an **in-memory store** and HMAC-signed URLs (restart-volatile, dev-only). For production, set either of:

- `S3_BUCKET=...` (with `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or instance role).
- `GCS_BUCKET=...` (with default ADC or service account).

When configured, every tool response surfaces a durable cloud URL as `image_url` and keeps the signed URL as `image_url_signed`. S3 wins if both are set.

Default URL TTL (signed only): 24h (`IMAGE_URL_TTL_SECONDS`). Signature: `hmac_sha256(key, "{asset_id}|{expires}")[:32]`.

- Bad signature → `404` (don't leak existence).
- Expired → `410`.
- Constant-time compare via `hmac.compare_digest`.

## Auth (v0.2)

`POST /upload` accepts an optional bearer token via the `MCP_BEARER_TOKEN` env var:

- Unset → endpoint is open (default; OK for single-tenant dev).
- Set → callers must send `Authorization: Bearer <token>`. Wrong / missing → `401`.

`GET /images/{asset_id}` stays HMAC-only — browsers don't send `Authorization` on `<img>` tags, and signed URLs are unguessable in practice. MCP tool calls inherit the transport's auth.

---

## Roadmap

- v0.1 ✅ — 5 core tools, deterministic text/CTA, asset_ids, HMAC-signed URLs.
- v0.2 ✅ — `composite_images`, S3/GCS, bearer-protected `/upload`.
- v0.3 ✅ — `create_infographic` (deterministic layout + AI background + composited text).
- v0.4 ✅ — `refine_image` via Responses API (forced `tool_choice`, chain via `previous_response_id` or `image_generation_call_id`).
- v1.0 ✅ — Studio UI (`/app`, `/api/{tool}`), companion skill (`mcp-gpt-image-2-companion/SKILL.md`), per-IP rate limit (`RATE_LIMIT_PER_MINUTE`) + Cloud Armor edge policy snippet in `CLAUDE.md`.

See `CLAUDE.md` for operator notes and routing decisions.
