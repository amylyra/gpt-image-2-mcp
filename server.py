"""gpt-image-2-mcp — FastMCP entrypoint.

Tool surface (v1.0 — 8 tools):
  - upload_image
  - generate_image       (INTAKE: aspect_ratio + quality)
  - edit_image
  - add_text_overlay     (deterministic by default — exact text, zero API cost)
  - add_link_cta         (deterministic by default — exact URL rendering)
  - composite_images     (INTAKE: aspect_ratio + quality)
  - create_infographic   (INTAKE: layout + quality; hybrid AI background + deterministic text)
  - refine_image         (Responses API; chain via previous_response_id or image_generation_call_id)

HTTP endpoints (outside MCP):
  - GET  /upload                              drag-drop form
  - POST /upload                              accept image, return asset_id + signed URL
  - GET  /images/{asset_id}?expires=&sig=     HMAC-verified image fetch
  - GET  /app                                 Studio UI (tool tabs + drag-drop)
  - POST /api/{tool}                          JSON shim around any registered MCP tool

Auth: optional MCP_BEARER_TOKEN guards /upload and /api/{tool}; /images/{id} is HMAC-only.
Rate limit: optional RATE_LIMIT_PER_MINUTE on the same mutating endpoints (single-instance).
Transports: HTTP (default, Cloud Run) or stdio (set MCP_TRANSPORT=stdio).
"""

from __future__ import annotations

import os
import sys

log = sys.stderr.write

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from mcp.server.fastmcp import FastMCP

from studio import register_routes
from tools import register_tools


def _check_required_env() -> None:
    missing = []
    if not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if not os.environ.get("IMAGE_URL_SIGNING_KEY"):
        missing.append("IMAGE_URL_SIGNING_KEY (generate with: openssl rand -hex 32)")
    if missing:
        log("ERROR: Required env vars missing: " + ", ".join(missing) + "\n")
        sys.exit(1)


def _instructions_template() -> str:
    """Build server instructions with the live PUBLIC_URL substituted in.

    Lazily computed at server boot so PUBLIC_URL set at the same time (Cloud
    Run deploy or local .env) is reflected without import-order surprises.
    """
    public = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if not public:
        host = os.environ.get("HOST", "localhost")
        if host == "0.0.0.0":
            host = "localhost"
        port = os.environ.get("PORT", "8080")
        public = f"http://{host}:{port}"

    return f"""\
gpt-image-2-mcp — post-edit / text / infographic / multi-reference image work.

## STOP — read before ANY image tool call
NEVER pass base64 strings or data: URIs to any tool parameter. They are too
large for MCP transport and WILL hang the connection. If the user pastes /
drags / mentions a local image, run an upload snippet (below) FIRST.

## Getting images into tools

All image params accept either an `http(s)` URL OR an `asset_id` (durable
opaque ref). `upload_image` returns both — prefer `asset_id` when chaining.

1. **Direct URL (http/https, S3, CDN)** → pass straight to the tool.
2. **Public Google Drive link** → pass straight to the tool (auto-rewritten).
3. **`asset_id` from a prior call** → pass straight to any image param.

### Pasted / dragged image — pick the path for YOUR environment

**A. claude.ai web OR Claude Cowork — Python tool (auto-discovers uploads folder)**

Works for both claude.ai web (`/mnt/user-data/uploads/`) and Cowork
(`~/Library/Application Support/Claude/local-agent-mode-sessions/<session>/uploads/`).
Run with the **Python** tool (NOT bash/shell):

```python
import urllib.request, json, os, time, glob
SERVER = '{public}'
candidates = ['/mnt/user-data/uploads'] + sorted(
    glob.glob(os.path.expanduser('~/Library/Application Support/Claude/local-agent-mode-sessions/*/uploads')),
    key=os.path.getmtime, reverse=True)
uploads = next((p for p in candidates if os.path.isdir(p)), None)
if not uploads:
    print(f'No uploads folder found. Upload manually at {{SERVER}}/upload')
else:
    files = sorted([f for f in os.listdir(uploads) if f.lower().endswith(('.png','.jpg','.jpeg','.webp'))],
                   key=lambda f: os.path.getmtime(os.path.join(uploads, f)), reverse=True)[:4]
    for i, fname in enumerate(files):
        with open(os.path.join(uploads, fname), 'rb') as fh: data = fh.read()
        req = urllib.request.Request(f'{{SERVER}}/upload', data=data, method='POST')
        for attempt in range(3):
            try:
                result = json.loads(urllib.request.urlopen(req, timeout=30).read())
                print(f'image{{i}}: asset_id={{result["asset_id"]}}  image_url={{result["image_url"]}}')
                break
            except Exception as e:
                if attempt < 2: time.sleep(2 ** attempt)
                else: print(f'image{{i}}: FAILED: {{e}}. Upload manually at {{SERVER}}/upload')
```

**B. Claude Code (CLI) — Bash tool, real local filesystem**

If the user gives you a real path on their machine, just upload it via curl:

```bash
curl -fsS -X POST --data-binary "@/full/path/to/image.jpg" \\
  "{public}/upload" \\
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('asset_id='+d.get('asset_id','')+'  image_url='+d.get('image_url',''))"
```

When this MCP runs locally as stdio, `upload_image(image='/full/path/to/file.jpg')`
also works — the server reads the file directly.

### Fallbacks (any environment)
- **Manual upload:** {public}/upload (drag-drop form).
- **Studio UI:** {public}/app (full tool surface — generate, edit, text, CTA,
  infographic, refine — with output preview and asset_id history).

If a snippet fails (503, network), do not retry blindly — direct the user to
{public}/upload or {public}/app.

Never fabricate URLs. Never start a local HTTP server.

## Intake before generate_image, composite_images, create_infographic — REQUIRED

When the user has not specified an aspect_ratio AND a quality, ask in a single
message before calling these tools:

- aspect_ratio: 1:1, 4:5, 5:4, 16:9, 9:16, 3:2, 2:3, social-square,
  social-vertical, social-horizontal, infographic-portrait,
  infographic-landscape (default depends on use case — propose one)
- quality: low, medium (default), high  (high → 4K rendering, costs more)

Skip intake when: the user already named ratio + quality; chaining from a prior
tool call with fixed values; calling edit_image, refine_image, add_text_overlay,
add_link_cta, or upload_image (those default to the source image's shape and
need no intake).

## Presenting images to the user — CRITICAL

Image-output tools return three blocks: inline `Image` thumbnails, a markdown
`render_md` string, and a `json_str` with metadata. The MCP host renders the
thumbnails inline, but your chat reply is the user-facing presentation. After
every image tool call, your assistant reply MUST:

1. Surface the markdown image embed `![](image_url)` for each result — pull
   the URL from `image_url` in the JSON metadata.
2. Surface a clickable `[Download image](image_url)` link below each embed.
3. Carry the **Save to Google Drive?** nudge from `render_md` through verbatim.
4. Add a brief, friendly description of what was generated or edited.

Do NOT dump raw JSON. Do NOT say "here is your image" without the actual embed
and download link. The download link is the user's reliable fallback when an
embed doesn't render.

To chain a generated image into another tool, prefer `asset_id` from the JSON
metadata (durable across URL expiry); `image_url` works too.

## Save to Google Drive — when the user replies 'save'

Image-output tools end their `render_md` with a **Save to Google Drive?** nudge.
If the user replies 'save', 'save to drive', 'save to google drive', or similar:

1. Fetch image bytes from the most recent `image_url` (durable cloud URL or
   `/images/<asset_id>?...`) using the Python tool:
   `urllib.request.urlopen(url, timeout=30).read()`.
2. Call the Google Drive MCP `create_file` tool with the bytes and a sensible
   filename (e.g. `gpt-image-2-<short-prompt-or-timestamp>.png`, MIME `image/png`).
3. Reply with the resulting Drive link so the user can open it.

If the Google Drive MCP isn't installed, tell the user — do not fabricate a
Drive URL.

## Routing

- For URLs / CTAs / exact text: prefer add_link_cta and add_text_overlay
  (deterministic, $0, character-perfect). Do NOT use generate_image to render
  user-supplied URLs or stats — gpt-image-2 may corrupt them.
- For infographics with stats / bullets / footers: use create_infographic.
  The model never sees the user's text content; layout slots compose deterministically.
- For raw artistic / photoreal generation from a prompt: prefer Nano Banana.
- For iterative refinement of a prior result: refine_image (Responses API).
"""


def build_server() -> FastMCP:
    _check_required_env()
    mcp = FastMCP(
        name="gpt-image-2-mcp",
        instructions=_instructions_template(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8080)),
        stateless_http=True,
    )
    register_tools(mcp)
    register_routes(mcp)
    return mcp


def main() -> None:
    mcp = build_server()
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    log(f"[gpt-image-2-mcp] starting (transport={transport})\n")
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http")


if __name__ == "__main__":
    main()
