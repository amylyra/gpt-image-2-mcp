# Image model migration — prepared, not deployed

The source default changes from `gpt-image-2` to candidate
`gpt-image-2.6-sunburst`. The existing `OPENAI_IMAGE_MODEL` override wins;
`gpt-image-2.6-flare` is the manual alternative. There is no automatic
cross-model fallback or extra paid retry.

Keep the service name, endpoint, eight MCP input schemas, storage, credentials,
and deterministic text/CTA paths. The only raster-size addition is `3:4`:
1152×1536 normally, 1536×2048 at high quality. Startup logs report the configured
image model without secrets. Existing deployment overrides must be checked;
updating source alone cannot replace an explicit old-model override.

Official references checked on November 12, 2026:

- [Deprecations](https://developers.openai.com/api/docs/deprecations):
  `gpt-image-2` and its April snapshot retire February 10, 2027.
- [Image generation](https://developers.openai.com/api/docs/guides/image-generation):
  Sunburst is intended for detailed visual work; Flare for iteration. Omitted
  complexity defaults to high. This patch preserves the existing request fields.
- [Image edit API](https://developers.openai.com/api/reference/resources/images/methods/edit):
  both candidate model IDs are listed for edits.

## Evidence

Before changes: `python -m pytest test_simulation.py -x -q` → 112 passed.

After changes: `python -m pytest test_simulation.py test_migration.py -x -q`
→ 133 passed, with three dependency warnings. Environment: Python 3.12,
OpenAI SDK 3.49.0, MCP 1.29.0, Pillow 13.0.0, pytest 9.2.0, httpx 0.28.1.

New checks cover environment override precedence, equality with the captured
eight-tool input schemas, six edit-path payloads for both candidate models,
ordered references, candidate counts, return metadata, signed URLs, SDK JSON and
multipart serialization, masks, rejection propagation, and both `3:4` sizes.
The schema fixture records a read-only `tools/list` response on November 12, 2026;
it is not deployment provenance or evidence of an upstream model parameter.

## Required before approval to deploy

1. Read the existing deployment's effective image-model environment setting,
   serving revision(s), image digest, and traffic split. Tie each serving revision
   to source. If it already uses a supported model and passes validation, keep it.
2. Verify candidate access in the same API organization. Run the existing smoke
   test separately with `OPENAI_IMAGE_MODEL` set to each candidate, using an
   approved test budget. The script's budget check is retrospective, not a hard
   billing cap. No live test was run in this review; no API key was available.
3. Replay real workflow-produced prompts and references through the actual host
   pipelines; review text fidelity, identity, skin detail, geometry, palette,
   latency, dimensions, and billed usage. Synthetic fixtures certify none of
   these visual properties. Validate custom sizes, high quality, and masks on the
   upstream API rather than inferring acceptance from a mocked transport.
4. Approve any deployment or model-setting change explicitly. For an existing
   old-model override, the intended single-variable update is
   `OPENAI_IMAGE_MODEL=gpt-image-2.6-sunburst`; preserve all other variables and
   secret bindings. Do not replace the whole environment. No infrastructure,
   credential, storage, IAM, or subscription change is required by this patch.

The separate `refine_image` Responses path keeps its existing mainline model and
tool selection. This review does not establish its effective image backend or
2.6 compatibility. Quality-dependent token counts can change, so existing rough
cost estimates are not a migration spending guarantee.
