### 2026-04-28 — openai SDK pin too loose: `proxies` httpx clash
`requirements.txt` says `openai>=1.50`, but 1.44–1.54 break against `httpx>=0.28` with `Client.__init__() got an unexpected keyword argument 'proxies'`. Pin `openai>=1.55` so a fresh `pip install -r requirements.txt` doesn't ship a broken combo.

### 2026-04-28 — `gpt-image-2` needs org verification (not billing)
First call returns 403 `PermissionDeniedError` with "Your organization must be verified". This is a separate flow from API credits — verify at platform.openai.com/settings/organization/general, propagation up to 15 min. README/CLAUDE.md should mention this so users don't chase a billing/quota red herring.

### 2026-04-28 — `gpt-image-1.5` fallback doesn't accept the shim's sizes
1.5 only supports 1024x1024, 1024x1536, 1536x1024, auto. The aspect-ratio shim returns 1152x2048 / 2048x1152 / 1280x1024 etc. for portrait/landscape aliases — those are valid for gpt-image-2 only. When falling back to 1.5, restrict to `1:1`, `2:3`, `3:2` (or 4K equivalents that resolve to those sizes).

### 2026-04-29 — claude.ai web custom connector only supports OAuth, not static Bearer
The "Add custom connector" form has Name + URL + OAuth Client ID/Secret — no header field. Static `Authorization: Bearer <token>` won't work. Either implement OAuth 2.0 (DCR + token endpoint), or leave `/mcp` open and gate via Cloud Armor / IP allowlist / non-guessable URL. Claude Code (CLI) does support `-H "Authorization: Bearer ..."`, so a header-gated MCP is CLI-only.
