# Brand assets

**Decision (Rob, 2026-10-06):** neutral brand icon, **not** the U-tec / Ultraloq logo.

Home Assistant 2026.3+ loads local brand images for custom integrations from
`custom_components/utec_locks/brand/` (PNG only). That folder ships in the
release zip. This top-level folder keeps the source SVG and the same PNGs.

| File | Purpose |
|------|---------|
| `icon.svg` | Source artwork (simple geometric lock, neutral colours) |
| `icon.png` | 256 x 256 raster used by Home Assistant |
| `icon@2x.png` | 512 x 512 raster (hDPI) |

The PNGs were rendered from `icon.svg` with headless Chrome. Before a HACS
default-list submission, also publish to
[home-assistant/brands](https://github.com/home-assistant/brands).
