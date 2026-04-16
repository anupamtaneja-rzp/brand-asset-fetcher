# How to Run the Brand Asset Pipeline v3

## What you'll get
A folder (e.g. `batch_1_assets/`) with:
- **Each brand's logo** (transparent, square, 500x500 minimum) — plus SVG if found
- **Shape previews** (circle, square, card) for each brand
- **review.html** — interactive review page with colour overrides, mark-as-final, categories, export
- **review.csv** — spreadsheet with colours, confidence scores, blending risks

### New in v3
- Auto-categorisation (Food & Beverage, Fashion, Electronics, Travel, etc.)
- Website text scraping (meta descriptions + homepage + about page)
- Logo validation (flags wrong images — bad aspect ratio, too few colours, too uniform)
- **Colour override** — pick any custom background colour per brand
- **Mark as final** — lock in brands you're happy with
- **Export** — download JSON or CSV of finalised brands only
- **Progress bar** — track how many brands you've reviewed

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
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets
```

**What happens:**
- Progress line for each brand (1/100, 2/100...)
- Takes ~10-20 minutes for 100 brands (fetching logos + AI background removal)
- Console symbols: ✅ = good, ⚠️ = needs attention, ❌ = couldn't find logo, [SVG] = vector found, [!] = logo flagged by validation

---

### Step 8: Open the review page
```
open batch_1_assets/review.html
```

---

## Using the review page

### Toolbar (top)
- **Shape toggle**: Square / Circle / Card — changes ALL cards at once
- **Size slider**: make cards bigger or smaller
- **Sort by**: Name, Confidence, Blending Risk, Source Tier
- **Category filter**: dropdown to show one category at a time
- **Filter buttons**: Finalized / Pending / Flagged / Logo Issues

### Per-card controls
- **Colour swatches**: click any swatch to preview that background colour
- **Colour picker** (paint icon): pick ANY custom colour with the native colour wheel
- **Mark as Final** (checkmark button): locks the brand with current colour — card gets a green border
- **Flag button**: mark brands needing manual attention

### When you're done reviewing
- Click **Export JSON** or **Export CSV** at the top — downloads ONLY the finalised brands with your chosen colours
- The progress bar shows how many brands you've finalised out of total

---

## Running subsequent batches

Once you're happy with batch 1:
```
python brand_asset_pipeline.py --input batch_2_brands.csv --output batch_2_assets
open batch_2_assets/review.html
```

And so on for batch_3 through batch_6.

---

## Fine-tuning background removal

If some logos have messy edges, try different models:

**Default (general purpose):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets
```

**isnet model (often better for logos with clean edges):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --rembg-model isnet-general-use
```

**Alpha matting (cleaner edges, slower):**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --alpha-matting
```

**Both together:**
```
python brand_asset_pipeline.py --input batch_1_brands.csv --output batch_1_assets --rembg-model isnet-general-use --alpha-matting
```

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

---

## Troubleshooting

**"command not found: python3"**
Install Python: https://www.python.org/downloads/

**"No module named rembg"**
Make sure you activated the venv first: `source brand_env/bin/activate`, then run Step 6 again.

**"externally-managed-environment" error**
You forgot to activate the venv. Run: `source brand_env/bin/activate`

**Script is very slow**
First brand takes longer (downloading the AI model). After that, ~5-10 seconds per brand.

**cairosvg install fails**
Try: `brew install cairo` first, then `pip install cairosvg`. Or skip it — SVGs will still be saved, just not rasterised.

**Lots of failures**
Some brands don't have good logos online. That's expected. The 8-tier waterfall catches most, but some niche brands will need manual sourcing.
