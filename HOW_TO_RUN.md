# How to Run the Brand Asset Pipeline v6

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's folder** containing a `candidates/` subfolder with ALL sourced logos (PNG and SVG, full quality, square format)
- **review.html** — interactive review page: pick the best logo per brand, recolour SVGs, flag issues, export
- **review.csv** — spreadsheet with colours, confidence scores, blending risks
- **pipeline_summary.json** — full processing results
- **pipeline.log** — detailed debug log (fetch errors, HTTP statuses, tier diagnostics)

### What changed in v6
- **All candidates saved as files** — every logo option from every tier is saved at full quality in `candidates/` subfolder. Not just the primary.
- **No more classic mode** — `--multi` flag removed. Every run sources from ALL 9 tiers automatically (up to 50 candidates per brand).
- **SVGs preserved as vectors** — SVGs are never rasterized for output. They're saved with a square viewBox. Rasterization only happens for preview thumbnails.
- **No downsizing** — images are never shrunk. A 1200px logo stays 1200px. Only upscaled if below 500px.
- **SVG recolouring** — recolour SVG logos directly (in the review UI or via Python).
- **Flag-aware export** — flagged brands get sorted into folders (`flagged_wrong_logo/`, `flagged_needs_upscaling/`, etc.) so engineering knows what needs attention.
- **Clean terminal** — progress shows candidate counts. All debug output goes to `pipeline.log`.
- **Priority: SVG > transparent raster > opaque raster** — the pipeline suggests the best candidate using this priority.

---

## Step-by-step

### Step 1: Open Terminal
Press `Cmd + Space`, type **Terminal**, hit Enter.

---

### Step 2: Go to wherever you saved the files
```
cd ~/Desktop
```

---

### Step 3: Files you need
- `brand_asset_pipeline.py` (the script)
- `batch_1_brands.csv` (or any batch file)

---

### Step 4: Create a virtual environment (one time only)
```
python3 -m venv brand_env
```

---

### Step 5: Activate the virtual environment
```
source brand_env/bin/activate
```
You need to run this every time you open a new Terminal window.

---

### Step 6: Install Python packages (one time only)
```
pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg
```

**Optional — Playwright for SPA sites** (recommended):
```
pip install playwright
python -m playwright install chromium
```

---

### Step 7: Run a batch

**Standard run (sequential):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets
```

**Faster run (parallel):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --threads 4
```

**Debug mode (verbose terminal output):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --threads 4 --log-level debug
```

**What happens:**
- Progress line for each brand showing candidate count and format breakdown
- Console symbols: ✅ = 3+ candidates, ⚠️ = 1-2 candidates, ❌ = no candidates found
- At the end: source effectiveness table showing both primary picks and total candidates per tier
- All debug info written to `pipeline.log` in the output folder

---

### Step 8: Open the review page
```
open batch_1_assets/review.html
```

---

## Using the review page

### Grid view (default)
- **Shape toggle**: Square / Circle / Card
- **Size slider**: make cards bigger or smaller
- **Sort by**: Name, Confidence, Blending Risk, Source Tier, Category
- **Category filter**: dropdown to show one category at a time
- **Filter buttons**: All / Finalized / Pending / Flagged / Wrong Logo / Needs Upscaling / Wrong Colour / Low risk / High risk / SVG / Logo issues
- **Colour swatches** on each card: click to change background colour
- **Mark as Final** button
- **Flag** button — choose a flag reason

### Expanded detail view (click any card)
- **Larger logo preview** with selected background
- **Logo picker** — all candidates shown with source, tier, format (SVG/PNG badge), dimensions, confidence. Click to switch.
- **SVG recolour** — for SVG logos, pick any colour. Applied directly to the SVG.
- **Background colour swatches**
- **Source info** — tier, URL, dimensions
- **Website link**

### Flag reasons
| Flag | Use when... |
|------|-------------|
| Wrong Logo | The image isn't the brand's logo |
| Needs Upscaling | Logo is too small or pixelated |
| Wrong Colour | Extracted brand colour doesn't match |
| Other | Any other issue |

### Exporting
- **Export JSON / CSV** — finalized brands with metadata (includes flag_reason, selected_file, format)
- **Export ZIP** — the key handoff artifact:
  - `final/` — unflagged finalized brands (one file each, full quality)
  - `flagged_wrong_logo/` — flagged but finalized
  - `flagged_needs_upscaling/` — flagged but finalized
  - `flagged_wrong_colour/` — flagged but finalized
  - `flagged_other/` — flagged but finalized
  - `brand_data.csv` + `brand_data.json`
  - SVGs with recolour baked in if you set one
  - Engineering gets: `final/` = ready to ship, `flagged_*/` = needs attention

---

## Output folder structure

```
batch_1_assets/
  saregama/
    candidates/
      00_brandfetch_logodev.png        # full quality, square
      01_website_svg-link.svg          # square viewBox SVG
      02_website_apple-touch-icon.png  # full quality, square
      03_simpleicons_svg.svg           # square viewBox SVG
    logo.png                           # primary (processed)
    logo.svg                           # primary SVG (if applicable)
    meta.json                          # all metadata + candidate file refs
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
| `--input FILE` | (required) | Input CSV file |
| `--output DIR` | `./brand_assets` | Output directory |
| `--sample N` | `0` (all) | Process N random brands (0 = all rows) |
| `--threads N` | `1` | Parallel threads (recommended: 4) |
| `--rembg-model MODEL` | `u2net` | BG removal model |
| `--alpha-matting` | off | Cleaner edges on BG removal (slower) |
| `--log-level LEVEL` | `info` | `info` = clean terminal, `debug` = verbose |

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

Every tier runs for every brand. All results saved as candidates. Pipeline suggests the best one using priority: SVG > transparent raster > opaque raster.

---

## Troubleshooting

**"command not found: python3"**
Install Python: https://www.python.org/downloads/

**"No module named rembg"**
Activate venv first: `source brand_env/bin/activate`, then run Step 6 again.

**Where are the debug logs?**
Check `pipeline.log` in the output folder. All fetch errors, HTTP statuses, and tier diagnostics are there.

**Script is slow**
First brand takes longer (downloading the AI model). Use `--threads 4` to speed up. Each brand now runs all tiers (more thorough but slower than v5's classic mode).

**cairosvg install fails**
Try: `brew install cairo` first, then `pip install cairosvg`. Without it, SVGs will be saved but won't get preview thumbnails in the HTML.
