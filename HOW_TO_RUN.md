# How to Run the Brand Asset Pipeline v6

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's folder** containing a `candidates/` subfolder with ALL sourced logos (PNG and SVG, full quality, square format)
- **review.html** — interactive review page served at `localhost:4200`: pick the best logo per brand, recolour SVGs, flag issues, save/load sessions, export
- **review.csv** — spreadsheet with colours, confidence scores, blending risks
- **pipeline_summary.json** — full processing results
- **pipeline.log** — detailed debug log (fetch errors, HTTP statuses, tier diagnostics)

### What changed in v6
- **All candidates saved as files** — every logo option from every tier is saved at full quality in `candidates/` subfolder
- **No more classic mode** — every run sources from ALL 9 tiers automatically (up to 50 candidates per brand)
- **SVGs preserved as vectors** — SVGs are never rasterized for output. Saved with square viewBox. Rasterization only for preview thumbnails.
- **No downsizing** — images are never shrunk. Only upscaled if below 500px.
- **SVG recolouring** — recolour SVG logos in the review UI. Baked into the file on export.
- **Flag-aware export** — flagged brands sorted into folders (`flagged_wrong_logo/`, etc.)
- **Reward Catalogue CSV** — export CSV matches the bulk creation template format
- **Save/Load sessions** — save your review progress to a JSON file. Resume anytime.
- **Local review server** — auto-starts after pipeline run. Full-quality exports work out of the box.
- **Remaining brands** — export includes a `remaining_brands.csv` listing everything not finalized
- **Improved SVG detection** — inline SVGs found via `<title>` matching, aria-label, and SPA framework detection (Remix, Gatsby, Next.js, Nuxt)
- **Clean terminal** — progress shows candidate counts. All debug output goes to `pipeline.log`.

---

## Quick start

### 1. Install (one time)
```bash
# Create virtual environment
python3 -m venv brand_env
source brand_env/bin/activate

# Install dependencies
pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg

# Optional — Playwright for SPA sites (recommended)
pip install playwright
python -m playwright install chromium
```

### 2. Run the pipeline
```bash
source brand_env/bin/activate
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets
```

The pipeline sources logos, processes them, generates the review page, and auto-opens it at `http://localhost:4200/review.html`. Press `Ctrl+C` to stop the server when done.

### 3. Resume a previous review session
```bash
python brand_asset_pipeline.py --serve --output batch_1_assets
```
Opens the review page without re-running the pipeline. Click **Load Session** to restore your saved progress.

---

## Run options

**Standard run:**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets
```

**Parallel (faster):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4
```

**Debug mode (verbose terminal):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --log-level debug
```

**Custom port:**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --port 8080
```

**Skip auto-server (just generate files):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --no-serve
```

**Resume review only:**
```bash
python brand_asset_pipeline.py --serve --output batch_1_assets
```

---

## Using the review page

### Grid view
- **Shape toggle**: Square / Circle / Card
- **Size slider**: make cards bigger or smaller
- **Sort by**: Name, Confidence, Blending Risk, Source Tier, Category
- **Category filter**: dropdown to show one category at a time
- **Filter buttons**: All / Finalized / Pending / Flagged / Wrong Logo / Needs Upscaling / Wrong Colour / Low risk / High risk / SVG / Logo issues
- **Colour swatches** on each card: click to change background colour
- **Mark as Final** and **Flag** buttons

### Detail view (click any card)
- **Logo picker** — all candidates with source, tier, format (SVG/PNG), dimensions, confidence. Click to switch.
- **SVG recolour** — for SVG logos, pick any colour. Baked into the export.
- **Background colour swatches**
- **Source info**, website link, quality metrics

### Save / Load session
- **Save Session** — downloads `review_session.json` with all your decisions (finalized, flagged, selected logos, recolours, colours)
- **Load Session** — pick a previously saved JSON to restore all state

### Flag reasons
| Flag | Use when... |
|------|-------------|
| Wrong Logo | The image isn't the brand's logo |
| Needs Upscaling | Logo is too small or pixelated |
| Wrong Colour | Extracted brand colour doesn't match |
| Other | Any other issue |

### Exporting
- **Export CSV** — Reward Catalogue format (15 standard columns + pipeline internal columns)
- **Export JSON** — same data, structured
- **Export ZIP** — the key handoff artifact:
  - `final/` — unflagged finalized brands (one logo each, full quality)
  - `flagged_wrong_logo/`, `flagged_needs_upscaling/`, `flagged_wrong_colour/`, `flagged_other/` — flagged but finalized
  - `brand_data.csv` — Reward Catalogue format for bulk upload
  - `brand_data.json` — structured version
  - `remaining_brands.csv` — all non-finalized brands with status and errors
  - SVGs have recolour baked in if you set one

---

## Export CSV format (Reward Catalogue)

| Column | Source |
|--------|--------|
| `category_display_name` | Scraped from website |
| `brand_name` | From input CSV |
| `brand_description` | Scraped meta description |
| `brand_url` | From input CSV |
| `brand_logo_url` | Filename of exported asset (e.g. `Saregama.svg`) |
| `brand_background_colour` | Reviewer's selected hex |
| `reward_display_name` | Same as brand_name |
| `offer_redemption_channel` | Empty (ops fills in) |
| `offer_redemption_url` | Same as brand_url |
| `reward_image_url` | Empty (separate featured image) |
| `reward_bgcolor_code` | Same as brand_background_colour |
| `translation_short_description` | Scraped meta description |
| `translation_description` | Empty (ops writes) |
| `translation_how_to_redeem` | Empty (ops writes) |
| `translation_terms_and_conditions` | Empty (ops writes) |

Pipeline internal columns appended after: `flag_reason`, `logo_format`, `logo_recolour`, `source_tier`, `confidence`, `blending_risk`, `logo_quality_score`, `logo_issues`, `original_size`, `undersize`, `bg_removed`.

---

## Output folder structure

```
batch_1_assets/
  saregama/
    candidates/
      00_brandfetch_logodev.png
      01_website_svg-link.svg
      02_website_apple-touch-icon.png
      03_simpleicons_svg.svg
    logo.png
    logo.svg
    meta.json
  another_brand/
    candidates/
      ...
    meta.json
  review.html
  review.csv
  pipeline_summary.json
  pipeline.log
```

---

## All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--input FILE` | (required*) | Input CSV file (*not needed with `--serve`) |
| `--output DIR` | `./brand_assets` | Output directory |
| `--sample N` | `0` (all) | Process N random brands |
| `--threads N` | `1` | Parallel threads (recommended: 4) |
| `--rembg-model MODEL` | `u2net` | BG removal model |
| `--alpha-matting` | off | Cleaner edges on BG removal (slower) |
| `--log-level LEVEL` | `info` | `info` = clean, `debug` = verbose |
| `--serve` | off | Start review server only (no pipeline) |
| `--no-serve` | off | Skip auto-server after pipeline run |
| `--port N` | `4200` | Port for the review server |

---

## Logo sourcing tiers (all run for every brand)

| Tier | Source | Type |
|------|--------|------|
| 0 | CSV-provided URL | Direct link |
| 1 | Brandfetch / logo.dev | CDN + API |
| 2 | Website scraping | HTML parsing + inline SVG |
| 2b | Playwright SPA fallback | Headless browser (optional) |
| 3 | Wikimedia Commons / Wikipedia | Search API |
| 4 | Google Favicon | Free API |
| 5 | DuckDuckGo | Instant Answer API |
| 6 | Gilbarbara SVG repo | GitHub (2000+ brands) |
| 7 | Seeklogo.com | Search + scrape |
| 8 | Simple Icons | CDN (3000+ SVGs) |

---

## Troubleshooting

**"command not found: python3"**
Install Python: https://www.python.org/downloads/

**"No module named rembg"**
Activate venv first: `source brand_env/bin/activate`, then install again.

**Port 4200 already in use**
The server auto-tries the next few ports. Or use `--port 8080`.

**Where are the debug logs?**
Check `pipeline.log` in the output folder.

**Script is slow**
First brand takes longer (downloading the AI model). Use `--threads 4`. Each brand runs all 9 tiers.

**cairosvg install fails**
Try: `brew install cairo` first, then `pip install cairosvg`.

**Export ZIP has thumbnails instead of full-quality images**
Make sure the review page is served via `localhost` (not opened as a local file). The pipeline auto-starts the server, but if you opened `review.html` directly, re-open via `--serve`.
