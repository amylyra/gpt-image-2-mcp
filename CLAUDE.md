# gpt-image-2-mcp — Operator Notes

## When to use this MCP vs Nano Banana

| Use this server when... | Use Nano Banana when... |
|---|---|
| Adding text / CTAs / URLs to existing images | Generating raw artistic / photoreal assets |
| Building infographics or text-heavy layouts | Style-driven photo composition |
| Identity-preserving edits across many references | Single subject, single reference |
| Multi-image compositing (up to 16) | Variations of a single source |
| Brand-consistent post-edit work | First-pass concept generation |

Both servers run independently. Route via tool descriptions — there is **no** shared coexistence layer.

## Response shape — three FastMCP blocks

Image-output tools return `[Image(thumb_jpeg), ..., render_md_str, json_str]`:

1. **`Image` blocks** — 1024px JPEG thumbnails. Render inline in the tool pane and chat reply.
2. **`render_md_str`** — markdown image embed + `[Download image](url)` link per result, ending with the **Save to Google Drive?** nudge.
3. **`json_str`** — full metadata as JSON. `unpack_tool_result()` (in `tools.py`) extracts this; the `/api/{tool}` shim uses it to produce clean REST responses.

The agent must surface the markdown embed + download link from `render_md` in its chat reply — the inline thumbnail isn't always reliable, but the markdown embed + download link always render. This is taught in three places that must stay aligned:

- Server `instructions` block (`## Presenting images to the user — CRITICAL`)
- `mcp-gpt-image-2-companion/SKILL.md`
- This section

`upload_image` also returns the block list (so users see a thumbnail confirmation), but skips the Save-to-Drive nudge — it's a utility, not a creative output.

## Save to Google Drive — when the user replies 'save'

Every image-output `render_md` ends with: *"Save to Google Drive? Reply *save* and I'll upload via the Google Drive MCP."* When the user replies *save* / *save to drive*, the agent fetches bytes from the most recent `image_url` (durable cloud URL or `/images/<asset_id>?...`) and calls Google Drive MCP's `create_file` with a sensible filename + MIME. If Google Drive MCP isn't installed, tell the user — never fabricate a Drive URL.

The nudge is generated in `_render_markdown()` (`tools.py`); the workflow is taught in three external places — all must stay aligned:
- Server `instructions` block (`## Save to Google Drive — when the user replies 'save'`)
- `mcp-gpt-image-2-companion/SKILL.md`
- This section

## Upload paths by environment

The remote Cloud Run server can't read the user's local filesystem, so each client has its own snippet. `upload_image` returns environment-aware fix-it messages on data-URI or invalid-path errors so the agent picks the right one without prompting.

| Environment | Tool | File source | Path |
|---|---|---|---|
| claude.ai web | Python | `/mnt/user-data/uploads/` | urllib snippet → `/upload` |
| Claude Cowork (Mac) | Python | `~/Library/Application Support/Claude/local-agent-mode-sessions/<session>/uploads/` | same urllib snippet (auto-discovers folder) |
| Claude Code (CLI) — remote MCP | Bash | user's real filesystem | `curl --data-binary "@/path"` → `/upload` |
| Claude Code — local stdio MCP | n/a | user's real filesystem | `upload_image(image='/path')` reads server-side |
| Any | n/a | n/a | `{PUBLIC_URL}/upload` (manual) or `{PUBLIC_URL}/app` (Studio) |

Helpers in `tools.py`:
- `_urllib_snippet()` — Python snippet for claude.ai web AND Claude Cowork. Auto-discovers the uploads folder by trying `/mnt/user-data/uploads`, then the most recently modified `~/Library/Application Support/Claude/local-agent-mode-sessions/*/uploads`. One snippet works in both environments.
- `_claude_code_snippet(path=...)` — Claude Code Bash curl one-liner. Substitutes the user-provided path when available so the agent runs it verbatim.

Both are returned together in `upload_image` data-URI / not-found errors so the agent picks the one matching its environment. Both auto-thread `MCP_BEARER_TOKEN` (Authorization header / `-H` flag) when the env var is set.

## Intake before image-generating tools

The agent must confirm `aspect_ratio` and `quality` (or `layout` + `quality` for infographics) with the user before calling these tools when the user hasn't already named both. Single-message intake:

- aspect_ratio: `1:1, 4:5, 5:4, 16:9, 9:16, 3:2, 2:3, social-square, social-vertical, social-horizontal, infographic-portrait, infographic-landscape`
- quality: `low, medium (default), high (4K — costs more)`

Intake applies to: `generate_image`, `composite_images`, `create_infographic` (those produce a NEW image whose shape is the user's call).

Intake does NOT apply to: `edit_image`, `refine_image`, `add_text_overlay`, `add_link_cta`, `upload_image` (output shape is determined by the source image).

For this claim to be load-bearing, `edit_image` MUST default to source shape — not 1:1. The implementation calls `_closest_aspect_size(source_w, source_h)` whenever `aspect_ratio` is unset. Don't reintroduce a `="1:1"` default; if you do, the intake-skip claim above becomes false and `TestEditImage::test_aspect_ratio_defaults_to_source_shape` will fail.

Lives in two places — both must stay aligned:
- Server `instructions` block in `server.py` (`## Intake before generate_image, composite_images, create_infographic — REQUIRED`)
- Tool docstrings (`INTAKE REQUIRED:` sentence + per-arg `Confirm with user.` notes)

## Deterministic vs AI-integrated mode

The `add_text_overlay` and `add_link_cta` tools default to **deterministic** mode:

- **Deterministic (default):** Pillow renders text directly onto the image. Output is PNG. Zero API cost. Text is exact, character-for-character. URLs are safe (`proven.com/spring` is `proven.com/spring`).
- **AI-integrated:** gpt-image-2 generates a new image with text embedded in the scene. Useful for signs, labels on products, or stylized composition. **Not character-perfect** — never use for URLs in user-facing assets.

Default is deterministic for a reason. If a user asks for "a CTA at the bottom" they almost always want pixel-perfect text, not a re-rendered image.

## create_infographic — the hybrid path

`create_infographic` is structurally deterministic for text:
1. Build a background prompt that describes ONLY the visual style and explicitly forbids text in the image.
2. Send to gpt-image-2 — the model never sees the user's title / bullets / stats / URL.
3. Composite the user's text on top via `typography.py`, using the slot bboxes from the chosen layout (`portrait-stat`, `landscape-comparison`, `square-list`).

This makes URL/stat fidelity structural, not prompt-engineered. If a user asks for an infographic, this is the right tool — not `generate_image`.

## Required env

- `OPENAI_API_KEY` — access to the selected image model required.
- `IMAGE_URL_SIGNING_KEY` — generate with `openssl rand -hex 32`. Server fails fast if missing.
- `PUBLIC_URL` (Cloud Run only) — without it, signed image URLs default to localhost.

## Models

- `OPENAI_IMAGE_MODEL=gpt-image-2.6-sunburst` (candidate default; requires live validation).
- Alternative (manual): `gpt-image-2.6-flare`. No automatic cross-model retries.
- `OPENAI_REFINE_MODEL=gpt-5.5` reserved for v0.4 (`refine_image` via Responses API).

## Preserved adapter contract — DO NOT pass these

- `response_format` — always base64.
- `thinking` — Responses API param, not Images.
- `input_fidelity` — intentionally omitted by this adapter.
- `background="transparent"` — not exposed by this adapter.
- `output_compression` with `output_format="png"` — invalid combo.

`openai_images.validate_image_params()` rejects all of the above before the SDK call.

## Storage limits (v0.1)

- In-memory only. Restart-volatile.
- `STORE_TTL=86400` (24h). `STORE_MAX_ITEMS=200`. Both env-tunable.
- Signed URL TTL also defaults to 24h via `IMAGE_URL_TTL_SECONDS`.
- For production durable URLs, wait for v0.2 (S3/GCS) or set `PUBLIC_URL` and accept the in-memory window.

## Asset IDs

Every image returned by any tool includes `asset_id` (prefixed `img_`). Pass it back as input to any tool. Tools route via `image_io.resolve_image_input`:
- starts with `img_` → store lookup
- http(s) URL with `/images/{id}` path → store lookup
- other http(s) URL → fetch with SSRF guards
- everything else → ValueError (data: URIs explicitly rejected)

## Rate limiting & auth

- `GET /images/{asset_id}` — HMAC URL signing only. Bad sig → 404, expired → 410.
- `POST /upload` (v0.2) — optional bearer token via `MCP_BEARER_TOKEN`. Unset = open (single-tenant dev). Set = `Authorization: Bearer <token>` required, wrong/missing → 401.
- **In-app rate limit (v1.0):** `RATE_LIMIT_PER_MINUTE=N` enables a per-IP sliding-window limit on `/upload` and `/api/{tool}`. Single-instance only (in-memory). Unset = unlimited.
- **Production rate limiting:** use Cloud Armor at the edge — see snippet below. The in-app limiter is a safety net for solo deployments, NOT a replacement for an edge policy.

### Cloud Armor rate-limit policy (production)

```bash
# 1) Create a security policy with a per-IP rate-based ban rule.
gcloud compute security-policies create gpt-image-2-rl \
  --description "Rate-limit gpt-image-2-mcp"

gcloud compute security-policies rules create 1000 \
  --security-policy=gpt-image-2-rl \
  --src-ip-ranges='*' \
  --action=rate-based-ban \
  --rate-limit-threshold-count=120 \
  --rate-limit-threshold-interval-sec=60 \
  --ban-duration-sec=300 \
  --conform-action=allow \
  --exceed-action=deny-429 \
  --enforce-on-key=IP

# 2) Bind to the Cloud Run service via a serverless NEG + backend service
#    (one-time per project — see GCP docs for the full chain).
gcloud compute backend-services update gpt-image-2-be \
  --security-policy=gpt-image-2-rl --global
```

Tune `--rate-limit-threshold-count` to your actual traffic. Cloud Armor enforces at the edge across all instances; the in-app limiter does not.

## Cloud storage (v0.2)

Set one of:
- `S3_BUCKET` (+ `AWS_REGION`, AWS creds via env or instance role).
- `GCS_BUCKET` (+ default ADC or service account).

S3 wins if both are set. Tool responses surface the durable URL as `image_url`; the signed in-memory URL is kept as `image_url_signed` for parity. Failed cloud upload → falls back silently to the signed URL (logged via the response shape — `image_url` will be the signed one).

## Cloud Run deploy with Secret Manager

```bash
# Create secrets (once)
echo -n "$OPENAI_API_KEY"        | gcloud secrets create gpt-image-2-openai --data-file=-
openssl rand -hex 32             | gcloud secrets create gpt-image-2-signing --data-file=-
echo -n "$MCP_BEARER_TOKEN"      | gcloud secrets create gpt-image-2-bearer --data-file=-

# Deploy
gcloud run deploy gpt-image-2-mcp --source . --region us-central1 --min-instances=1 \
  --update-secrets=OPENAI_API_KEY=gpt-image-2-openai:latest,IMAGE_URL_SIGNING_KEY=gpt-image-2-signing:latest,MCP_BEARER_TOKEN=gpt-image-2-bearer:latest \
  --set-env-vars=PUBLIC_URL=https://your.run.app,GCS_BUCKET=your-bucket
```

`--min-instances=1` is required — without it cold-start 503s break the urllib upload path used by claude.ai web clients.

## Code map

- `server.py` — FastMCP entrypoint, env check, transport selection.
- `tools.py` — `@mcp.tool` definitions (v0.1 + v0.2 + v0.3 + v0.4 = 8 tools).
- `openai_images.py` — Image API adapter, size shim, param validation.
- `openai_responses.py` — Responses API adapter (v0.4 `refine_image`); forced `tool_choice`, chain handles.
- `deterministic_render.py` — PIL text overlay + CTA renderer (single-line).
- `typography.py` — multi-line typography blocks (titles, bullets, stats).
- `infographic.py` — layout templates + create_infographic pipeline.
- `image_io.py` — URL fetch, SSRF, asset routing, normalize, mask.
- `storage.py` — in-memory store, asset_id, signed URL, S3/GCS upload.
- `security.py` — HMAC sign / verify, bearer check (constant-time).
- `studio.py` — `/upload` form + endpoint, `/images/{id}` HMAC handler, `/app` Studio UI, `/api/{tool}` JSON shim.
- `costs.py` — pricing table + estimator + actuals from usage.
- `prompts.py` — versioned AI-mode fallback templates.
- `mcp-gpt-image-2-companion/SKILL.md` — Claude skill that routes post-edit/text/infographic work here vs. NanoBanana.

## After any code change

Update together:
- `README.md` — keep tool list, env vars, response shape in sync.
- `CLAUDE.md` — keep behavior summary, contracts, routing accurate.
- `test_simulation.py` — patch tests for changed behavior; add new tests for new features.
- `smoke_openai_contract.py` — keep banned-param list aligned with `openai_images.validate_image_params`.

## Testing

```bash
python -m pytest test_simulation.py -x -q
python smoke_openai_contract.py --cheap   # ~$0.02–$0.05 per run
```


Migration review and release gates: see `MIGRATION_REVIEW.md`. The legacy service name is not proof of the selected upstream model. Preserve the eight tool schemas and existing storage/authentication.
