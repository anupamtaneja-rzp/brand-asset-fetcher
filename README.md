# Brand Asset Fetcher

Automated pipeline that sources brand logos from 9 online tiers, processes them into square-format assets, and generates an interactive HTML review page for human QA before engineering handoff.

Built for Razorpay's reward catalogue — takes a CSV of brand names, outputs production-ready logo assets with a Reward Catalogue-compatible CSV.

## What it does

1. **Sources logos** from 9 tiers per brand: Brandfetch, website scraping (including inline SVGs and SPAs via Playwright), Wikimedia, Google Favicon, DuckDuckGo, Gilbarbara, Seeklogo, Simple Icons
2. **Processes** each logo: square padding, background removal (rembg), colour extraction (k-means), transparency detection, quality scoring
3. **Generates an interactive review page** at `localhost:4200` where you pick the best logo per brand, recolour SVGs, flag issues, and export a ZIP
4. **Exports** a Reward Catalogue-format CSV + logo files sorted by flag status, ready for engineering

## Quick start

```bash
pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg

# Run pipeline + auto-open review page
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4

# Resume review later (no re-scraping)
python brand_asset_pipeline.py --serve --output batch_1_assets
```

## Review page features

- Grid view with shape/size/sort/category/filter controls
- Detail view with all logo candidates per brand (click to switch)
- SVG recolouring (baked into export)
- Background colour picker with blending risk indicators
- Flag system: wrong logo, needs upscaling, wrong colour, other
- Save/Load session (JSON file) — close browser, resume later
- Export ZIP with flag-sorted folders + Reward Catalogue CSV + remaining brands list

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--input FILE` | required* | Input CSV (*not needed with `--serve`) |
| `--output DIR` | `./brand_assets` | Output directory |
| `--sample N` | all | Process N random brands |
| `--threads N` | 1 | Parallel threads (recommended: 4) |
| `--serve` | off | Start review server only |
| `--no-serve` | off | Skip auto-server after run |
| `--port N` | 4200 | Review server port |
| `--rembg-model` | u2net | BG removal model |
| `--alpha-matting` | off | Cleaner BG removal edges |
| `--log-level` | info | info or debug |

## Input CSV format

Minimum columns: `brand_name` (or `name`). Optional: `business_website` (or `url`), `logo_url`, `colour`.

## Output

```
batch_1_assets/
  brand_folder/
    candidates/          # All sourced logos at full quality
      00_website_svg-link.svg
      01_brandfetch_logodev.png
      ...
    logo.png             # Primary processed logo
    meta.json            # Metadata
  review.html            # Interactive review page
  review.csv             # Pipeline results
  pipeline.log           # Debug log
```

See [HOW_TO_RUN.md](HOW_TO_RUN.md) for detailed usage, export format, and troubleshooting.
