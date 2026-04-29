# How to Run the Brand Asset Pipeline v7

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's folder** containing a `candidates/` subfolder with ALL sourced logos (PNG and SVG, full quality, **pre-padded square canvas with 12px padding**)
- **review.html** — interactive review page served at `localhost:4200`: pick the best logo per brand, recolour SVGs, flag issues, save/load sessions, export
- **review.csv** — spreadsheet with colours, confidence scores, blending risks
- **pipeline_summary.json** — full processing results
- **pipeline.log** — detailed debug log (fetch errors, HTTP statuses, tier diagnostics)

After finalizing (separate step):
- **`<output>_final.zip`** — production-quality assets with upscaled rasters and proper SVG recolouring

### What changed in v7
- **Two-stage processing**: light pass during sourcing (padding/squaring), heavy pass on shortlisted finalists only (upscaling + DOM-based SVG recolour). Saves time — you don't upscale candidates you'll reject.
- **Server-side SVG processing** — proper XML parsing replaces the old regex approach. Recolouring now handles inline `style=""`, `<style>` blocks, gradient stops, and CSS classes correctly.
- **Auto padding & square canvas** — every candidate (PNG and SVG) gets 12px padding on a square canvas during sourcing. What you see in review is what you'll ship.
- **Upscayl integration** — rasters under 500px get 4x upscaled via `realesrgan-ncnn-vulkan` during finalize. Edge-preserving model tuned for logos.
- **`--finalize` command** — new mode that reads your saved review session and produces the production-quality ZIP for engineering handoff.
- **Skip-upscale toggle** — per-brand opt-out in review UI for intentionally low-res sources (pixel art, etc.)

### Carried over from v6
- All candidates saved at full quality in `candidates/` subfolder; sources from all 9 tiers automatically
- SVGs preserved as vectors throughout
- Save/Load review sessions to a JSON file
- Local review server on port 4200; full-quality exports work out of the box
- Flag-aware export with sorted folders
- Reward Catalogue CSV format for engineering bulk upload
- `remaining_brands.csv` listing non-finalized brands
- Improved SVG detection (Remix, Gatsby, Next.js, Nuxt)

---

## The reviewer's full workflow (3 phases)

### Phase 1 — Source & open review (5–15 min)
```bash
source brand_env/bin/activate
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --threads 4
```
Pipeline sources logos from all 9 tiers, pads each candidate with 12px on a square canvas, and auto-opens the review page at `http://localhost:4200/review.html`.

### Phase 2 — Review in browser (~30 min for 80 brands)
- Pick the best candidate per brand (or flag with reason)
- Optionally pick a recolour for SVGs (live preview)
- Optionally toggle **Skip upscale** for any brand whose raster you want to keep low-res
- Click **Save Session** every 10–15 min — downloads `review_session.json` to your Downloads folder
- When done, **move `review_session.json` into the `batch_1_assets/` folder**
- Press `Ctrl+C` in the terminal to stop the server

### Phase 3 — Finalize for engineering handoff (5–10 min)
```bash
python brand_asset_pipeline.py --finalize batch_1_assets --upscale --threads 4
```
Reads your session, runs Upscayl on rasters under 500px, applies SVG recolouring properly via DOM parsing, re-pads everything, and outputs `batch_1_assets_final.zip`. Send that ZIP to engineering.

---

## Install (one time)
```bash
# Create virtual environment
python3 -m venv brand_env
source brand_env/bin/activate

# Install Python dependencies
pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg lxml

# Optional — Playwright for SPA sites (recommended)
pip install playwright
python -m playwright install chromium

# Required for upscaling — choose one:
# Option A: brew install realesrgan-ncnn-vulkan
# Option B: download Upscayl from https://upscayl.org/ — pipeline auto-detects the bundled binary
```

If neither Upscayl install path works, you can still run the pipeline with `--no-upscale` and finalize will skip upscaling (rasters keep their original size).

### Resume a previous review session
```bash
python brand_asset_pipeline.py --serve --output batch_1_assets
```
Opens the review page without re-running the pipeline. Click **Load Session** to restore your saved progress.

---

## Run options

**Standard sourcing run (Phase 1):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4
```

**Finalize for handoff (Phase 3, after review):**
```bash
python brand_asset_pipeline.py --finalize batch_1_assets --upscale --threads 4
```

**Finalize without upscaling:**
```bash
python brand_asset_pipeline.py --finalize batch_1_assets --threads 4
```

**Custom padding / canvas size:**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4 --padding 12 --canvas-size 512
```

**Debug mode (verbose terminal):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4 --log-level debug
```

**Custom port:**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4 --port 8080
```

**Skip auto-server (just generate files):**
```bash
python brand_asset_pipeline.py --input brands.csv --output batch_1_assets --threads 4 --no-serve
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

### Exporting from the browser (quick preview)
The in-browser **Export ZIP** button uses the pre-padded files from sourcing. Use this to share drafts. **It does not upscale rasters or apply DOM-based SVG recolouring** — those happen only in Phase 3.

- **Export CSV** — Reward Catalogue format (15 standard columns + pipeline internal columns)
- **Export JSON** — same data, structured
- **Export ZIP (preview)** — quick draft, no upscaling, regex-based SVG recolour

### Production handoff (Phase 3)
Run `--finalize` to get the production-quality ZIP with proper upscaling and SVG processing:
```bash
python brand_asset_pipeline.py --finalize batch_1_assets --upscale --threads 4
```

The output `batch_1_assets_final.zip` contains:
- `final/` — unflagged finalized brands (upscaled rasters, properly recoloured SVGs)
- `flagged_wrong_logo/`, `flagged_needs_upscaling/`, `flagged_wrong_colour/`, `flagged_other/` — flagged but finalized
- `brand_data.csv` — Reward Catalogue format for bulk upload
- `brand_data.json` — structured version
- `remaining_brands.csv` — all non-finalized brands with status and errors
- `finalize_report.txt` — per-brand log of what was upscaled, what colour was applied, any warnings

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
| `--input FILE` | (required*) | Input CSV file (*not needed with `--serve` or `--finalize`) |
| `--output DIR` | `./brand_assets` | Output directory |
| `--sample N` | `0` (all) | Process N random brands |
| `--threads N` | `1` | Parallel threads (recommended: 4) |
| `--rembg-model MODEL` | `u2net` | BG removal model |
| `--alpha-matting` | off | Cleaner edges on BG removal (slower) |
| `--log-level LEVEL` | `info` | `info` = clean, `debug` = verbose |
| `--serve` | off | Start review server only (no pipeline) |
| `--no-serve` | off | Skip auto-server after pipeline run |
| `--port N` | `4200` | Port for the review server |
| `--padding N` | `12` | Pixels of padding around the logo on the square canvas |
| `--canvas-size N` | `512` | Output square canvas size (px) for normalized assets |
| `--finalize DIR` | — | Run Phase 3 on a sourced+reviewed folder. Reads `review_session.json` from the folder. |
| `--upscale` | off | Enable Upscayl 4x upscaling for rasters under 500px (Phase 3 only) |
| `--no-upscale` | — | Explicitly disable upscaling (overrides `--upscale`) |
| `--upscale-threshold N` | `500` | Minimum dimension (px) below which a raster gets upscaled |
| `--upscale-model NAME` | `realesrgan-x4plus-anime` | Upscayl model — `anime` preserves logo edges best |
| `--upscayl-bin PATH` | (auto) | Override path to `realesrgan-ncnn-vulkan` binary if auto-detect fails |

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

**`--finalize` says "Upscayl not found"**
Either install via `brew install realesrgan-ncnn-vulkan`, or download Upscayl from https://upscayl.org/ and the pipeline will auto-detect the bundled binary. Or just run `--finalize` without `--upscale` to skip upscaling — rasters will keep their original resolution.

**`--finalize` says "review_session.json not found"**
After reviewing in the browser, click **Save Session** and move the downloaded JSON file into your output folder (e.g. `batch_1_assets/review_session.json`). Then re-run `--finalize`.

**SVG recolour looks wrong in the final ZIP**
The `--finalize` command uses proper DOM parsing — it should handle inline styles, CSS blocks, and gradients correctly. If a specific SVG still looks wrong, check `finalize_report.txt` in the output for that brand's processing log. Worst case, the pipeline preserves the original SVG so you can hand-fix it.
