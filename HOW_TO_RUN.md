# How to Run the Brand Asset Pipeline v4

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's logo** (transparent, square, 500x500 minimum) — plus SVG if found
- **review.html** — interactive review page with expandable cards, logo picker, colour overrides, SVG recolour, mark-as-final, and export (JSON / CSV / ZIP)
- **review.csv** — spreadsheet with colours, confidence scores, blending risks
- **pipeline_summary.json** — full processing results

### New in v4
- **Expandable detail panel** — click any card to see full info, pick between logo options, recolour SVGs, compare sources
- **Multi-candidate logos** — `--multi` flag collects up to 5 logo options per brand from different sources
- **Parallel processing** — `--threads 4` processes brands concurrently (much faster)
- **Seeklogo.com** — new Tier 7 source for vector logos (9 tiers total)
- **ZIP export** — downloads a zip with all finalized logos + data files
- **Source effectiveness report** — tells you which tiers are working and which have zero hits
- **Website links** on each card — click to verify against the brand's actual site

---

## Step-by-step (copy-paste each command)

### Step 1: Open Terminal
Press `Cmd + Space`, type **Terminal**, hit Enter.

---

### Step 2: Go to wherever you saved the files
If you saved the pipeline files to your Desktop:
```
cd ~/Desktop
```
(Change this if you saved them somewhere else.)

---

### Step 3: Put your files together
You need these files in the same folder:
- `brand_asset_pipeline.py` (the script)
- `batch_1_brands.csv` (batch 1 — 100 tier-2 brands)

The other batch files (`batch_2_brands.csv` through `batch_6_brands.csv`) are for later.

---

### Step 4: Create a virtual environment (one time only)
```
python3 -m venv brand_env
```
This creates a little sandbox for Python packages. Takes 5 seconds.

---

### Step 5: Activate the virtual environment
```
source brand_env/bin/activate
```
You'll see `(brand_env)` appear at the start of your terminal line. That means it worked.

**Important:** You need to run this command every time you open a new Terminal window.

---

### Step 6: Install Python packages (one time only)
```
pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg
```
This downloads everything including the AI model for background removal (~170MB). Takes 2-3 minutes.

**Note:** `cairosvg` is for SVG rendering. If it fails to install, that's okay — the pipeline works without it, you just won't get SVG rasterisation. Try `brew install cairo` first if it fails.

---

### Step 7: Run batch 1

**Quick run (first match wins, sequential):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets
```

**Recommended run (multi-candidate logos + parallel processing):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --multi --threads 4
```

**What happens:**
- Progress line for each brand (1/100, 2/100...)
- Quick run: ~10-20 minutes for 100 brands
- Multi + threads: ~15-20 minutes (more work per brand, but parallel)
- Console symbols: ✅ = good, ⚠️ = needs attention, ❌ = couldn't find logo, [SVG] = vector found, [3 opts] = multiple logo options found
- At the end: source effectiveness table showing hits per tier

---

### Step 8: Open the review page
```
open batch_1_assets/review.html
```

---

## Using the review page

### Grid view (default)
- **Shape toggle**: Square / Circle / Card — changes ALL cards at once
- **Size slider**: make cards bigger or smaller
- **Sort by**: Name, Confidence, Blending Risk, Source Tier, Category
- **Category filter**: dropdown to show one category at a time
- **Filter buttons**: All / Finalized / Pending / Flagged / Low risk / High risk / SVG / Logo issues
- **Colour swatches** on each card: click to change background colour
- **Mark as Final / Flag** buttons on each card

### Expanded detail view (click any card)
- **Larger logo preview** with the selected background colour
- **Logo picker** — if you used `--multi`, you'll see all logo options side by side. Click one to switch.
- **SVG recolour** — for SVG logos, one-click White/Black or pick any colour. The logo's fills get replaced live.
- **Background colour swatches** — bigger, easier to compare
- **Source info** — tier, source URL, dimensions, confidence
- **Website link** — opens the brand's website in a new tab
- Press **Escape** or click outside to close

### Exporting
- **Export JSON** — finalized brands with full metadata
- **Export CSV** — same data, flat format
- **Export ZIP** — downloads a zip containing: `logos/Brand_Name.png` for each finalized brand, plus `brand_data.csv` and `brand_data.json`
- The progress bar shows how many brands you've finalized out of total

---

## Running subsequent batches

Once you're happy with batch 1:
```
python brand_asset_pipeline.py --input batch_2_brands.csv --output batch_2_assets --multi --threads 4
open batch_2_assets/review.html
```

And so on for batch_3 through batch_6.

---

## All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--input FILE` | (required) | Input CSV file |
| `--output DIR` | `./brand_assets` | Output directory |
| `--sample N` | `0` (all) | Process N random brands (0 = all rows) |
| `--multi` | off | Collect up to 5 logo candidates per brand from multiple tiers |
| `--threads N` | `1` | Parallel threads (recommended: 4) |
| `--rembg-model MODEL` | `u2net` | BG removal model: `u2net`, `u2net_human_seg`, `isnet-general-use` |
| `--alpha-matting` | off | Cleaner edges on BG removal (slower) |

---

## Logo sourcing tiers (in order)

| Tier | Source | Type |
|------|--------|------|
| 0 | CSV-provided URL | Direct link |
| 1 | Brandfetch / logo.dev | CDN + API |
| 2 | Website scraping | HTML parsing |
| 3 | Wikimedia Commons / Wikipedia | Search API |
| 4 | Google Favicon | Free API |
| 5 | DuckDuckGo | Instant Answer API |
| 6 | Gilbarbara SVG repo | GitHub (2000+ brands) |
| 7 | Seeklogo.com | Search + scrape |
| 8 | Simple Icons | CDN (3000+ SVGs) |

Without `--multi`, the script stops at the first tier that finds a logo. With `--multi`, it collects from all tiers (up to 5 candidates) so you can pick the best one.

---

## What the badges mean

| Badge | Meaning |
|-------|---------|
| Green **LOW** | Blending risk low — logo contrasts well with background |
| Orange **MEDIUM** | Might need manual check |
| Red **HIGH** | Logo colours too similar to background |
| Purple **BG REMOVED** | AI removed the background |
| Blue **SVG** | Vector logo found (best quality) |
| Yellow **UPSCALED** | Original logo was under 500px |
| Red **!** | Logo validation flagged issues (wrong image?) |
| Grey **N opts** | Number of logo candidates available (with --multi) |

---

## Troubleshooting

**"command not found: python3"**
Install Python: https://www.python.org/downloads/

**"No module named rembg"**
Make sure you activated the venv first: `source brand_env/bin/activate`, then run Step 6 again.

**"externally-managed-environment" error**
You forgot to activate the venv. Run: `source brand_env/bin/activate`

**Script is very slow**
First brand takes longer (downloading the AI model). After that, ~5-10 seconds per brand. Use `--threads 4` to speed up.

**cairosvg install fails**
Try: `brew install cairo` first, then `pip install cairosvg`. Or skip it — SVGs will still be saved, just not rasterised.

**Lots of failures**
Some brands don't have good logos online. That's expected. Check the source effectiveness table at the end — if a tier has zero hits, it may not be useful for your brand set.

**ZIP export button doesn't work**
The ZIP feature requires internet access (loads JSZip from CDN). If you're offline, use JSON or CSV export instead.
