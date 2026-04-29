# How to Run the Brand Asset Pipeline v7.2

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's folder** containing a `candidates/` subfolder with ALL sourced logos. The padded preview is a `.png`; the **raw original** is preserved in its native format (`.webp`, `.jpg`, `.png`, `.svg`, etc.) inside `candidates/raw/`.
- **review.html** — interactive review page served at `localhost:4200`: pick the best logo per brand, recolour SVGs, monochromize rasters, mark which ones to upscale, flag issues, save/load sessions, export
- **review.csv** — spreadsheet with colours, confidence scores, blending risks
- **pipeline_summary.json** — full processing results
- **input_rows.json** — original CSV rows indexed by brand name (for `merchant_id` / `merchant_email` passthrough at finalize)
- **pipeline.log** — detailed debug log (fetch errors, HTTP statuses, tier diagnostics)

After finalizing (separate step):
- **`<output>_final.zip`** — production-quality assets. Logos are passed through in their original format when no manipulation is needed (WebP stays WebP, JPEG stays JPEG, etc.) — and converted to PNG only when you've requested recolour, monochromize, or upscale.

### What changed in v7.2
- **Original raster format preserved end-to-end** — WebP/JPEG/AVIF logos stay as `.webp`/`.jpg`/`.avif` in raw/ AND in the final ZIP (when no manipulation is requested). Zero re-encoding loss. Pass-through copy at finalize when reviewer didn't recolour/monochromize/upscale.
- **Upscaling is now opt-in per brand** — pipeline never auto-flags. Default: nothing upscales. Reviewer ticks "Upscale this logo 4x at finalize" in the detail panel for any brand they want upscaled. Resolution badge in the grid shows `↑4x` only on opted-in logos.
- **`merchant_id` and `merchant_email` passthrough** — extra columns from your input CSV (e.g. `merchant_id`, `merchant_email`) are carried through to the final `brand_data.csv`, so you can map back to your CRM.
- **WebP/JPEG/AVIF accepted from Wikimedia** — tier 3 search no longer hard-filters to SVG+PNG.
- **SVG bbox letterbox fix** — non-square SVGs (wordmarks like Vodafone/IndiGo) no longer render off-centre or cropped after viewBox normalization.

### What changed in v7 (carried forward)
- **Two-stage processing**: light pass during sourcing (padding/squaring), heavy pass on shortlisted finalists only (upscaling + DOM-based SVG recolour). Saves time — you don't upscale candidates you'll reject.
- **Server-side SVG processing** — proper XML parsing replaces the old regex approach. Recolouring now handles inline `style=""`, `<style>` blocks, gradient stops, and CSS classes correctly.
- **Auto padding & square canvas** — every candidate gets 12px padding on a square canvas during sourcing. What you see in review is what you'll ship.
- **Upscayl integration** — `realesrgan-ncnn-vulkan` via the bundled Upscayl app binary. Edge-preserving model tuned for logos.
- **`--finalize` command** — separate mode that reads your saved review session and produces the production-quality ZIP for engineering handoff.
- **Raster monochromize** — alpha-preserving lossless silhouette conversion to black or white via in-browser preview, baked at finalize.

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
For each brand in the grid:
- Pick the best candidate (or flag it with a reason)
- (SVG only) Pick a **recolour** — live preview shows the new colour
- (Raster only) Pick a **logo colour** — `Original` / `Black silhouette` / `White silhouette`. Lossless, alpha-preserving.
- (Raster only) Tick **"Upscale this logo 4x at finalize"** for any logo whose source resolution is too low. Default is OFF — only the ones you tick get upscaled.
- (Optional) Set a **background colour** for the catalogue card

Periodically click **Save Session** — downloads `review_session.json` to your Downloads folder. When fully done, **move `review_session.json` into the `batch_1_assets/` folder**, then press `Ctrl+C` in the terminal to stop the server.

### Phase 3 — Finalize for engineering handoff (5–10 min)
```bash
python brand_asset_pipeline.py --finalize batch_1_assets --upscale --threads 4
```
Reads your session and:
- For SVGs: applies DOM-based recolour (handles inline styles, gradient stops, etc.) and writes `.svg`
- For rasters you opted into upscaling: runs Upscayl 4x, then applies monochromize/pad as requested, writes `.png`
- For rasters with no manipulation requested: **pass-through copy** of the original file in its native format (`.webp` stays `.webp`, etc.)
- Builds `batch_1_assets_final.zip` with the Reward Catalogue CSV (including `merchant_id` and `merchant_email` passthrough)

Send that ZIP to engineering.

> **Tip:** if you don't pass `--upscale`, the pipeline still runs and just warns about any brands you opted in. Use that for a "no upscale" preview run, then re-finalize with `--upscale` once Upscayl is installed.

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

# Optional — for upscaling rasters at finalize time:
#  Option A (recommended): download Upscayl from https://upscayl.org/ → drag to Applications.
#                          Pipeline auto-detects /Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin
#  Option B: brew install --cask upscayl  (uses the official cask; same auto-detection as Option A)
```

If Upscayl isn't installed, `--finalize` runs fine without `--upscale`. Any logos you opted into upscaling get a warning in the report and pass through at original resolution.

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

Pipeline internal columns appended after the standard 15: `flag_reason`, `logo_format` (actual file extension — `svg` / `webp` / `jpg` / `png`), `logo_recolour`, `logo_monochrome`, `upscaled` (yes/no), `passthrough` (yes/no — true when the original file was copied verbatim), `source_tier`, `source_name`, `original_size`.

**Input CSV passthrough columns:** `merchant_id` and `merchant_email` from your input CSV are carried through to `brand_data.csv` automatically — no extra config. Any other extra columns in your input are ignored by the pipeline but available in `input_rows.json` if you want to add them to the final CSV later.

---

## Output folder structure

```
batch_1_assets/
  saregama/
    candidates/
      raw/                              ← original files, untouched
        00_brandfetch_logodev.webp      ← format preserved (was WebP at source)
        01_website_svg-link.svg
        02_website_apple-touch-icon.png
        03_simpleicons_svg.svg
      00_brandfetch_logodev.png         ← padded preview (always PNG)
      01_website_svg-link.svg           ← normalized SVG (viewBox padded)
      02_website_apple-touch-icon.png
      03_simpleicons_svg.svg
    logo.png
    logo.svg
    meta.json
  another_brand/
    candidates/
      raw/...
      ...
    meta.json
  review.html
  review.csv
  pipeline_summary.json
  input_rows.json                       ← original CSV rows (for passthrough)
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
Easiest fix: download Upscayl from https://upscayl.org/ → drag to Applications. Pipeline auto-detects `/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin`. Alternatively `brew install --cask upscayl`. Or just run `--finalize` without `--upscale` to skip upscaling — rasters keep their original resolution and any opted-in brands get a warning in the report.

**No logos got upscaled even though I expected them to**
Two things must both be true: (1) you ticked "Upscale this logo 4x at finalize" in the review UI for those brands (defaults to OFF in v7.2), AND (2) you passed `--upscale` to the finalize command. Both are required. Check `finalize_report.txt` for per-brand status.

**`--finalize` says "review_session.json not found"**
After reviewing in the browser, click **Save Session** and move the downloaded JSON file into your output folder (e.g. `batch_1_assets/review_session.json`). Then re-run `--finalize`.

**SVG recolour looks wrong in the final ZIP**
The `--finalize` command uses proper DOM parsing — it should handle inline styles, CSS blocks, and gradients correctly. If a specific SVG still looks wrong, check `finalize_report.txt` in the output for that brand's processing log. Worst case, the pipeline preserves the original SVG so you can hand-fix it.
