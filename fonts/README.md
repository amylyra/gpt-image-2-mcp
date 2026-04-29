# Fonts

Drop OFL-licensed font files here for higher-quality typography in deterministic text overlays.

Recommended:
- `Inter-Regular.ttf`, `Inter-Bold.ttf` — UI / headlines / body
- `IBMPlexSans-Regular.ttf`, `IBMPlexSans-Bold.ttf` — alternative body
- `JetBrainsMono-Regular.ttf` — URL / monospace text in CTAs

Sources:
- Inter: https://github.com/rsms/inter/releases
- IBM Plex Sans: https://github.com/IBM/plex/releases
- JetBrains Mono: https://github.com/JetBrains/JetBrainsMono/releases

The deterministic renderer in `deterministic_render.py` looks for these by name. If none are present, it falls back to common system fonts (DejaVuSans on Linux, Helvetica/Arial on macOS) and finally to PIL's default bitmap font. The fallback chain means the server runs without bundled fonts — but production-quality text rendering needs them.
