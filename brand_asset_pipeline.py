#!/usr/bin/env python3
"""
Brand Asset Pipeline — PoC v2
===============================
Automatically sources brand logos (SVG preferred), removes backgrounds,
extracts brand colours, and generates an interactive HTML review page.

Sourcing tiers (in order):
    1. Brandfetch (logo.dev CDN + search API)
    2. Website scraping (apple-touch-icon, og:image, SVG, img-logo, favicon)
    3. Wikimedia Commons + Wikipedia (often has SVG logos for well-known brands)
    4. Google Favicon API (low quality, but wide coverage)
    5. DuckDuckGo Instant Answer (free, returns brand images)
    6. Gilbarbara SVG logo repo (2000+ brand SVGs on GitHub)
    7. Simple Icons (3000+ brand SVGs + brand colours)

Setup (one time):
    python3 -m venv brand_env && source brand_env/bin/activate
    pip install requests beautifulsoup4 Pillow scikit-learn numpy rembg onnxruntime cairosvg

Run:
    python brand_asset_pipeline.py --input brands.csv --sample 50

Options:
    --rembg-model MODEL    Background removal model (default: u2net)
                           Choices: u2net, u2net_human_seg, isnet-general-use
    --alpha-matting        Enable alpha matting for cleaner edges (slower)
    --sample N             Process N random brands (0 = all)
    --output DIR           Output directory (default: ./brand_assets)

Input CSV must have columns: brand_name, business_website
Output: ./brand_assets/<brand>/  with logo.png, logo.svg (if found), meta.json
        ./brand_assets/review.html   (interactive review page)
        ./brand_assets/review.csv    (spreadsheet for review)
"""

import argparse, csv, json, os, re, sys, time, hashlib, base64
from pathlib import Path
from urllib.parse import urljoin, urlparse
from io import BytesIO
from collections import Counter

import requests
from bs4 import BeautifulSoup
from PIL import Image
import numpy as np
from sklearn.cluster import KMeans
from rembg import remove as rembg_remove

# Check cairosvg availability once at startup
try:
    import cairosvg
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False


# ─── SAFE JSON ENCODER ─────────────────────────────────────────────────────────

class SafeEncoder(json.JSONEncoder):
    """Handle numpy types that json.dump chokes on."""
    def default(self, o):
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return super().default(o)


# ─── CONFIG ────────────────────────────────────────────────────────────────────
TIMEOUT = 15
MIN_LOGO_SIZE = 200       # reject raster images smaller than this during sourcing
TARGET_SIZE = 500         # output logo size (minimum guaranteed)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) BrandAssetBot/1.0"}
OUT_DIR = Path("./brand_assets")
REMBG_MODEL = "u2net"
ALPHA_MATTING = False


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def _domain_from_url(url: str) -> str:
    """Extract clean domain from URL."""
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    domain = parsed.netloc or parsed.path
    return domain.replace("www.", "").strip("/")


def _is_svg_url(url: str) -> bool:
    """Check if URL points to an SVG."""
    return ".svg" in urlparse(url).path.lower()


def _fetch_svg(url: str) -> bytes | None:
    """Download SVG content and validate it."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        content = resp.content
        # Basic SVG validation
        text = content.decode("utf-8", errors="ignore")[:2000]
        if "<svg" in text.lower():
            return content
        return None
    except:
        return None


def _svg_to_pil(svg_bytes: bytes, size: int = 500) -> Image.Image | None:
    """Rasterise SVG to PIL Image at given size. Requires cairosvg."""
    if not HAS_CAIROSVG:
        return None
    try:
        png_data = cairosvg.svg2png(bytestring=svg_bytes, output_width=size, output_height=size)
        return Image.open(BytesIO(png_data)).convert("RGBA")
    except Exception as e:
        print(f"[svg] cairosvg failed: {e}", flush=True)
        return None


def _fetch_image(url: str) -> tuple[Image.Image | None, bool, bytes | None]:
    """
    Download an image URL. Returns (PIL Image, is_svg, svg_raw_bytes).
    svg_raw_bytes is set even if rasterization failed, so we can still save the SVG.
    """
    # SVG handling
    svg_data = None
    if _is_svg_url(url):
        svg_data = _fetch_svg(url)
        if svg_data:
            img = _svg_to_pil(svg_data)
            if img:
                return img, True, svg_data
            # SVG found but can't rasterize — fall through to try as raster
            # (some .svg URLs actually return raster fallbacks)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        if resp.status_code != 200:
            return None, False, svg_data
        ct = resp.headers.get("Content-Type", "")
        # Check if response is SVG
        if "svg" in ct:
            svg_data = resp.content
            img = _svg_to_pil(svg_data)
            if img:
                return img, True, svg_data
            # Have SVG data but can't rasterize — return None image but keep svg_data
            return None, True, svg_data
        if "image" not in ct and "octet" not in ct:
            return None, False, svg_data
        img = Image.open(BytesIO(resp.content))
        if img.width < MIN_LOGO_SIZE or img.height < MIN_LOGO_SIZE:
            return None, False, svg_data
        return img, False, svg_data
    except:
        return None, False, svg_data


# ─── TIER 1: BRANDFETCH ─────────────────────────────────────────────────────

def tier1_brandfetch(brand_name: str, website: str = "") -> dict | None:
    """
    Brandfetch: search API → get icon URL + domain.
    The search endpoint is free and returns an icon per result.
    We also try the img.logo.dev free endpoint (powered by Brandfetch data).
    """
    domain = _domain_from_url(website)

    # Strategy 1a: logo.dev free endpoint (returns good quality PNGs/SVGs by domain)
    if domain:
        for logo_url in [
            f"https://img.logo.dev/{domain}?token=pk_anonymous&size=512&format=png",
            f"https://logo.clearbit.com/{domain}",  # may still work for some
        ]:
            try:
                resp = requests.get(logo_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                ct = resp.headers.get("Content-Type", "")
                if "svg" in ct:
                    img = _svg_to_pil(resp.content)
                    if img:
                        return {
                            "source": "brandfetch:logodev-svg",
                            "image": img, "svg_data": resp.content,
                            "is_svg": True, "url": logo_url,
                            "domain": domain, "confidence": 0.92,
                        }
                elif "image" in ct:
                    img = Image.open(BytesIO(resp.content))
                    if img.width >= MIN_LOGO_SIZE and img.height >= MIN_LOGO_SIZE:
                        return {
                            "source": "brandfetch:logodev",
                            "image": img, "is_svg": False,
                            "url": logo_url, "domain": domain,
                            "confidence": 0.85,
                        }
            except:
                continue

    # Strategy 1b: Brandfetch search API (free, no key)
    try:
        slug = re.sub(r'[^a-z0-9]', '', brand_name.lower())
        resp = requests.get(
            f"https://api.brandfetch.io/v2/search/{slug}",
            headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code != 200:
            return None

        results = resp.json()
        if not isinstance(results, list) or len(results) == 0:
            return None

        best = results[0]
        icon_url = best.get("icon")
        found_domain = best.get("domain", "")

        # If search gave us a domain we didn't have, try logo.dev with it
        if found_domain and found_domain != domain:
            try:
                ld_url = f"https://img.logo.dev/{found_domain}?token=pk_anonymous&size=512&format=png"
                ld_resp = requests.get(ld_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
                if ld_resp.status_code == 200:
                    ct = ld_resp.headers.get("Content-Type", "")
                    if "image" in ct:
                        img = Image.open(BytesIO(ld_resp.content))
                        if img.width >= MIN_LOGO_SIZE:
                            return {
                                "source": "brandfetch:logodev-via-search",
                                "image": img, "is_svg": False,
                                "url": ld_url, "domain": found_domain,
                                "brand_name_api": best.get("name", brand_name),
                                "confidence": 0.82,
                            }
            except:
                pass

        # Fall back to the icon from search result
        if icon_url:
            img, is_svg, svg_raw = _fetch_image(icon_url)
            if img:
                result = {
                    "source": "brandfetch:search-icon",
                    "image": img, "is_svg": is_svg,
                    "url": icon_url, "domain": found_domain,
                    "brand_name_api": best.get("name", brand_name),
                    "confidence": 0.6,
                }
                if svg_raw:
                    result["svg_data"] = svg_raw
                return result
    except:
        pass

    return None


# ─── TIER 2: WEBSITE SCRAPING ────────────────────────────────────────────────

def tier2_website_scrape(website_url: str) -> dict | None:
    """Scrape brand website for logo images + theme-color. Prefers SVGs."""
    if not website_url or "play.google.com" in website_url:
        return None

    try:
        resp = requests.get(website_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        base = resp.url
    except:
        return None

    candidates = []

    # Priority 0 (HIGHEST): SVG logo links anywhere
    for link in soup.find_all("link", href=True):
        href = link["href"]
        if _is_svg_url(href):
            rel = " ".join(link.get("rel", []))
            if any(kw in rel.lower() for kw in ["icon", "logo", "shortcut"]) or \
               any(kw in href.lower() for kw in ["logo", "brand"]):
                candidates.append(("svg-link", urljoin(base, href), 0.95))

    for img_tag in soup.find_all("img", src=True):
        src = img_tag["src"]
        if _is_svg_url(src):
            alt = img_tag.get("alt", "")
            cls = " ".join(img_tag.get("class", []))
            if any("logo" in x.lower() for x in [src, alt, cls]):
                candidates.append(("svg-img", urljoin(base, src), 0.93))

    # Priority 1: apple-touch-icon
    for link in soup.find_all("link", rel=lambda r: r and "apple-touch-icon" in " ".join(r).lower()):
        href = link.get("href")
        if href:
            candidates.append(("apple-touch-icon", urljoin(base, href), 0.85))

    # Priority 2: og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        candidates.append(("og:image", urljoin(base, og["content"]), 0.6))

    # Priority 3: <img> tags with "logo" in class/alt/src (non-SVG, caught above)
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src", "")
        alt = img_tag.get("alt", "")
        cls = " ".join(img_tag.get("class", []))
        if any("logo" in x.lower() for x in [src, alt, cls]):
            full_url = urljoin(base, src)
            if not _is_svg_url(full_url):  # SVGs already added above
                candidates.append(("img-logo", full_url, 0.7))

    # Priority 4: large favicon
    for link in soup.find_all("link", rel=lambda r: r and "icon" in " ".join(r).lower()):
        href = link.get("href")
        sizes = link.get("sizes", "")
        if href:
            full_url = urljoin(base, href)
            if _is_svg_url(full_url):
                continue  # already handled
            size_val = 0
            m = re.search(r'(\d+)', sizes)
            if m:
                size_val = int(m.group(1))
            conf = 0.5 + min(size_val / 512, 0.3)
            candidates.append(("favicon", full_url, conf))

    # Extract theme-color
    theme_color = None
    tc = soup.find("meta", attrs={"name": "theme-color"})
    if tc and tc.get("content"):
        theme_color = tc["content"].strip()

    # Try candidates in priority order
    candidates.sort(key=lambda x: -x[2])
    for source_type, url, confidence in candidates:
        img, is_svg, svg_raw = _fetch_image(url)
        if img:
            result = {
                "source": f"website:{source_type}",
                "image": img,
                "is_svg": is_svg,
                "url": url,
                "confidence": confidence,
                "theme_color": theme_color,
            }
            if svg_raw:
                result["svg_data"] = svg_raw
            return result

    return None


# ─── TIER 3: WIKIMEDIA COMMONS + WIKIPEDIA ──────────────────────────────────

def tier3_wikimedia(brand_name: str) -> dict | None:
    """
    Search Wikimedia Commons for brand logos — often high-quality SVGs.
    Also checks Wikipedia page images as fallback.
    """
    # Strategy 3a: Wikimedia Commons search for SVG logos
    try:
        query = f"{brand_name} logo"
        api_url = (
            "https://commons.wikimedia.org/w/api.php"
            f"?action=query&list=search&srsearch={requests.utils.quote(query)}"
            "&srnamespace=6&srlimit=5&format=json"
        )
        resp = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("query", {}).get("search", [])
            for hit in hits:
                title = hit.get("title", "")
                # Only want SVG files or high-quality PNGs
                if not any(title.lower().endswith(ext) for ext in [".svg", ".png"]):
                    continue
                # Skip if title suggests it's not a logo (e.g. "Map of...", "Photo of...")
                title_lower = title.lower()
                if any(skip in title_lower for skip in ["map ", "photo ", "flag ", "coat of arms"]):
                    continue

                # Get the actual file URL
                file_url = _wikimedia_file_url(title)
                if not file_url:
                    continue

                is_svg = title.lower().endswith(".svg")
                if is_svg:
                    svg_data = _fetch_svg(file_url)
                    if svg_data:
                        img = _svg_to_pil(svg_data)
                        if img:
                            return {
                                "source": "wikimedia:commons-svg",
                                "image": img, "svg_data": svg_data,
                                "is_svg": True, "url": file_url,
                                "confidence": 0.8,
                            }
                else:
                    img, _, _ = _fetch_image(file_url)
                    if img:
                        return {
                            "source": "wikimedia:commons-png",
                            "image": img, "is_svg": False,
                            "url": file_url, "confidence": 0.7,
                        }
    except:
        pass

    # Strategy 3b: Wikipedia page image (from infobox)
    try:
        wp_url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=query&titles={requests.utils.quote(brand_name)}"
            "&prop=pageimages&format=json&pithumbsize=500&pilicense=any"
        )
        resp = requests.get(wp_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200:
            pages = resp.json().get("query", {}).get("pages", {})
            for pid, page in pages.items():
                if pid == "-1":
                    continue
                thumb = page.get("thumbnail", {}).get("source")
                if thumb:
                    # Wikipedia sometimes serves SVGs rendered as PNG thumbs.
                    # Try to get the original file which might be SVG.
                    original = page.get("pageimage", "")
                    found_svg_data = None
                    if original.lower().endswith(".svg"):
                        svg_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{requests.utils.quote(original)}"
                        found_svg_data = _fetch_svg(svg_url)
                        if found_svg_data:
                            img = _svg_to_pil(found_svg_data)
                            if img:
                                return {
                                    "source": "wikimedia:wikipedia-svg",
                                    "image": img, "svg_data": found_svg_data,
                                    "is_svg": True, "url": svg_url,
                                    "confidence": 0.75,
                                }
                            # SVG exists but can't rasterize — use thumb but keep SVG
                    # Fall back to thumbnail (carry SVG data if we found it)
                    img, is_svg, svg_raw = _fetch_image(thumb)
                    if img:
                        result = {
                            "source": "wikimedia:wikipedia-thumb",
                            "image": img, "is_svg": is_svg,
                            "url": thumb, "confidence": 0.6,
                        }
                        if found_svg_data:
                            result["svg_data"] = found_svg_data
                            result["is_svg"] = True  # we have the SVG even if image is raster
                        return result
    except:
        pass

    return None


def _wikimedia_file_url(title: str) -> str | None:
    """Get the direct file URL for a Wikimedia Commons file title."""
    try:
        api_url = (
            "https://commons.wikimedia.org/w/api.php"
            f"?action=query&titles={requests.utils.quote(title)}"
            "&prop=imageinfo&iiprop=url&format=json"
        )
        resp = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        pages = resp.json().get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if pid == "-1":
                continue
            info = page.get("imageinfo", [{}])[0]
            return info.get("url")
    except:
        return None


# ─── TIER 4: GOOGLE FAVICON API ──────────────────────────────────────────────

def tier4_google_favicon(website_url: str) -> dict | None:
    """Use Google's free Favicon API as last resort."""
    if not website_url:
        return None
    try:
        domain = _domain_from_url(website_url)
        if not domain:
            return None
        url = f"https://www.google.com/s2/favicons?domain={domain}&sz=256"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        img = Image.open(BytesIO(resp.content))
        if img.width < 32:
            return None
        return {
            "source": "google-favicon",
            "image": img,
            "is_svg": False,
            "url": url,
            "confidence": 0.4,
        }
    except:
        return None


# ─── TIER 5: DUCKDUCKGO INSTANT ANSWER ──────────────────────────────────────

def tier5_duckduckgo(brand_name: str) -> dict | None:
    """DuckDuckGo Instant Answer API — free, no key, returns brand images."""
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": brand_name, "format": "json", "no_html": 1, "skip_disambig": 1},
            headers=HEADERS, timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

        # Try Image field (usually the best logo source)
        img_url = data.get("Image")
        if img_url:
            if img_url.startswith("/"):
                img_url = "https://duckduckgo.com" + img_url
            img, is_svg, svg_raw = _fetch_image(img_url)
            if img:
                r = {
                    "source": "duckduckgo",
                    "image": img, "is_svg": is_svg,
                    "url": img_url, "confidence": 0.55,
                }
                if svg_raw:
                    r["svg_data"] = svg_raw
                return r

        # Try Infobox image
        infobox = data.get("Infobox", {})
        if isinstance(infobox, dict):
            for item in infobox.get("content", []):
                if item.get("data_type") == "image":
                    iurl = item.get("value", "")
                    if iurl:
                        img, is_svg, svg_raw = _fetch_image(iurl)
                        if img:
                            return {
                                "source": "duckduckgo:infobox",
                                "image": img, "is_svg": is_svg,
                                "url": iurl, "confidence": 0.5,
                            }
    except:
        pass
    return None


# ─── TIER 6: GILBARBARA SVG LOGOS ───────────────────────────────────────────

def tier6_gilbarbara(brand_name: str) -> dict | None:
    """
    Gilbarbara's logo repo on GitHub — 2000+ brand SVGs.
    CDN: https://raw.githubusercontent.com/gilbarbara/logos/main/logos/{slug}.svg
    Slug is lowercase, spaces→hyphens, no special chars.
    """
    slug = re.sub(r'[^a-z0-9\-]', '', brand_name.lower().replace(" ", "-").replace("&", "and"))

    # Try several slug variations
    variations = [slug]
    # Try without trailing 's' (e.g. "superliving" vs "superlivings")
    if slug.endswith("s") and len(slug) > 4:
        variations.append(slug[:-1])
    # Try with common suffixes removed
    for suffix in ["-india", "-in", "-com"]:
        if slug.endswith(suffix):
            variations.append(slug[: -len(suffix)])

    for s in variations:
        url = f"https://raw.githubusercontent.com/gilbarbara/logos/main/logos/{s}.svg"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("Content-Type", "")
            if "svg" in ct or "<svg" in resp.text[:500].lower():
                img = _svg_to_pil(resp.content)
                if img:
                    return {
                        "source": "gilbarbara-svg",
                        "image": img, "svg_data": resp.content,
                        "is_svg": True, "url": url,
                        "confidence": 0.75,
                    }
        except:
            continue

    return None


# ─── TIER 7: SIMPLE ICONS ──────────────────────────────────────────────────

def tier7_simple_icons(brand_name: str) -> dict | None:
    """
    Simple Icons — 3000+ brand SVGs. Free CDN.
    https://cdn.simpleicons.org/{slug}
    Returns single-colour SVGs. We also get the brand colour from their API.
    """
    slug = re.sub(r'[^a-z0-9]', '', brand_name.lower())

    # Simple Icons CDN returns SVG directly
    url = f"https://cdn.simpleicons.org/{slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        if "<svg" not in resp.text[:500].lower():
            return None
        img = _svg_to_pil(resp.content)
        if not img:
            return None

        # Also try to get the brand colour from Simple Icons JSON
        si_colour = None
        try:
            json_url = f"https://raw.githubusercontent.com/simple-icons/simple-icons/develop/_data/simple-icons.json"
            jresp = requests.get(json_url, headers=HEADERS, timeout=8)
            if jresp.status_code == 200:
                icons = jresp.json().get("icons", [])
                for icon in icons:
                    if icon.get("slug", "").lower() == slug or \
                       re.sub(r'[^a-z0-9]', '', icon.get("title", "").lower()) == slug:
                        si_colour = f"#{icon['hex']}"
                        break
        except:
            pass

        return {
            "source": "simpleicons-svg",
            "image": img, "svg_data": resp.content,
            "is_svg": True, "url": url,
            "confidence": 0.65,
            "si_colour": si_colour,  # bonus: brand colour from their DB
        }
    except:
        return None


# ─── IMAGE PROCESSING ────────────────────────────────────────────────────────

def has_transparency(img: Image.Image) -> bool:
    """Check if image has actual transparency."""
    if img.mode == "RGBA":
        arr = np.array(img)
        return bool((arr[:, :, 3] < 250).any())
    if img.mode == "P":
        return img.info.get("transparency") is not None
    return False


def remove_background(img: Image.Image) -> Image.Image:
    """Remove background using rembg. Returns RGBA with transparent bg."""
    buf_in = BytesIO()
    img.save(buf_in, format="PNG")

    kwargs = {"data": buf_in.getvalue(), "model_name": REMBG_MODEL}
    if ALPHA_MATTING:
        kwargs["alpha_matting"] = True
        kwargs["alpha_matting_foreground_threshold"] = 240
        kwargs["alpha_matting_background_threshold"] = 10

    buf_out = rembg_remove(**kwargs)
    return Image.open(BytesIO(buf_out)).convert("RGBA")


def auto_crop_transparent(img: Image.Image, padding_pct: float = 0.08) -> Image.Image:
    """Crop away empty transparent borders, then re-add a small padding."""
    arr = np.array(img.convert("RGBA"))
    alpha = arr[:, :, 3]
    rows = np.any(alpha > 10, axis=1)
    cols = np.any(alpha > 10, axis=0)
    if not rows.any() or not cols.any():
        return img
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    cropped = img.crop((cmin, rmin, cmax + 1, rmax + 1))

    # Re-add padding
    cropped = cropped.convert("RGBA")
    cw, ch = cropped.size
    if cw < 1 or ch < 1:
        return img
    pad = int(max(cw, ch) * padding_pct)
    padded = Image.new("RGBA", (cw + pad * 2, ch + pad * 2), (0, 0, 0, 0))
    padded.paste(cropped, (pad, pad), cropped)
    return padded


def process_logo(img: Image.Image) -> Image.Image:
    """Make logo square with padding, resize to at least TARGET_SIZE."""
    img = img.convert("RGBA")

    # Make square
    w, h = img.size
    if w != h:
        size = max(w, h)
        padded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        padded.paste(img, ((size - w) // 2, (size - h) // 2), img)
        img = padded

    # Resize — always to TARGET_SIZE (upscale if needed)
    if img.width != TARGET_SIZE:
        img = img.resize((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)

    return img


# ─── COLOUR EXTRACTION ──────────────────────────────────────────────────────

def extract_brand_colours(img: Image.Image, theme_color: str = None,
                          si_colour: str = None) -> dict:
    """
    Extract multiple BG colour candidates. Returns:
      {
        "primary": "#HEX",        # best guess
        "candidates": [           # all options (always includes white)
          {"hex": "#HEX", "source": "...", "blending_risk": "..."},
          ...
        ]
      }
    Sources: theme-color meta tag, Simple Icons DB, k-means clusters, white.
    """
    candidates = []

    # ── Candidate: theme-color from website ──────────────────────────────────
    if theme_color:
        tc = theme_color.strip().lstrip("#")
        if re.match(r'^[0-9a-fA-F]{3,6}$', tc):
            if len(tc) == 3:
                tc = "".join(c * 2 for c in tc)
            candidates.append({"hex": f"#{tc.upper()}", "source": "theme-color"})

    # ── Candidate: Simple Icons colour ───────────────────────────────────────
    if si_colour:
        sc = si_colour.strip().lstrip("#")
        if re.match(r'^[0-9a-fA-F]{6}$', sc):
            hex_val = f"#{sc.upper()}"
            if not any(c["hex"] == hex_val for c in candidates):
                candidates.append({"hex": hex_val, "source": "simpleicons"})

    # ── K-means clusters from logo pixels ────────────────────────────────────
    rgba = img.convert("RGBA")
    arr = np.array(rgba)

    mask = (arr[:, :, 3] > 200)
    mask &= ~((arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240))
    mask &= ~((arr[:, :, 0] < 15)  & (arr[:, :, 1] < 15)  & (arr[:, :, 2] < 15))

    pixels = arr[mask][:, :3]
    if len(pixels) >= 50:
        n_clusters = min(5, max(2, len(pixels) // 10))
        kmeans = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
        kmeans.fit(pixels)

        labels, counts = np.unique(kmeans.labels_, return_counts=True)
        centres = kmeans.cluster_centers_

        scored = []
        for i, (label, count) in enumerate(zip(labels, counts)):
            c = centres[i]
            brightness = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
            if brightness > 230 or brightness < 25:
                continue
            max_c, min_c = max(c), min(c)
            saturation = (max_c - min_c) / (max_c + 1)
            score = count * (1 + saturation * 2)
            scored.append((score, c))

        scored.sort(key=lambda x: -x[0])
        for rank, (score, c) in enumerate(scored[:3]):
            rgb = c.astype(int)
            hex_val = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
            if not any(c_["hex"] == hex_val for c_ in candidates):
                label = "kmeans-primary" if rank == 0 else f"kmeans-{rank+1}"
                candidates.append({"hex": hex_val, "source": label})

    # ── Always include white as a safe fallback ──────────────────────────────
    if not any(c["hex"] == "#FFFFFF" for c in candidates):
        candidates.append({"hex": "#FFFFFF", "source": "white"})

    # ── If nothing else, add a dark grey ─────────────────────────────────────
    if len(candidates) <= 1:
        candidates.insert(0, {"hex": "#333333", "source": "fallback"})

    # ── Calculate blending risk for each candidate ───────────────────────────
    for cand in candidates:
        risk = _blending_risk_for_hex(img, cand["hex"])
        cand["blending_risk"] = risk

    # ── Pick primary: prefer theme/simpleicons, then lowest-risk kmeans ──────
    primary = candidates[0]["hex"]  # default: first (theme-color or SI)
    # If the top candidate has HIGH blending risk, try to find a better one
    if candidates[0].get("blending_risk") == "HIGH":
        for c in candidates:
            if c["blending_risk"] in ("LOW", "MEDIUM") and c["source"] != "white":
                primary = c["hex"]
                break

    return {
        "primary": primary,
        "primary_source": next((c["source"] for c in candidates if c["hex"] == primary), "unknown"),
        "candidates": candidates,
    }


def _blending_risk_for_hex(logo_img: Image.Image, bg_hex: str) -> str:
    """Quick blending risk check for a single hex colour."""
    if bg_hex == "#FFFFFF":
        return "LOW"  # white is almost always safe for coloured logos
    try:
        bg_rgb = tuple(int(bg_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        rgba = np.array(logo_img.convert("RGBA"))
        opaque = rgba[rgba[:, :, 3] > 200][:, :3]
        if len(opaque) == 0:
            return "unknown"
        diffs = np.sqrt(np.sum((opaque.astype(float) - np.array(bg_rgb)) ** 2, axis=1))
        close_pct = float((diffs < 50).sum() / len(opaque))
        if close_pct > 0.3:
            return "HIGH"
        elif close_pct > 0.1:
            return "MEDIUM"
        return "LOW"
    except:
        return "unknown"


def validate_bg_colour(logo_img: Image.Image, bg_hex: str) -> dict:
    """Check if bg colour appears in the logo (blending risk). Full version."""
    if not bg_hex:
        return {"blending_risk": "unknown"}

    bg_rgb = tuple(int(bg_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    rgba = np.array(logo_img.convert("RGBA"))
    opaque = rgba[rgba[:, :, 3] > 200][:, :3]

    if len(opaque) == 0:
        return {"blending_risk": "unknown"}

    diffs = np.sqrt(np.sum((opaque.astype(float) - np.array(bg_rgb)) ** 2, axis=1))
    close_pct = float((diffs < 50).sum() / len(opaque))

    if close_pct > 0.3:
        return {"blending_risk": "HIGH", "pct_similar": round(close_pct * 100, 1)}
    elif close_pct > 0.1:
        return {"blending_risk": "MEDIUM", "pct_similar": round(close_pct * 100, 1)}
    else:
        return {"blending_risk": "LOW", "pct_similar": round(close_pct * 100, 1)}


def validate_bg_colour(logo_img: Image.Image, bg_hex: str) -> dict:
    """Check if bg colour appears in the logo (blending risk)."""
    if not bg_hex:
        return {"blending_risk": "unknown"}

    bg_rgb = tuple(int(bg_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    rgba = np.array(logo_img.convert("RGBA"))
    opaque = rgba[rgba[:, :, 3] > 200][:, :3]

    if len(opaque) == 0:
        return {"blending_risk": "unknown"}

    diffs = np.sqrt(np.sum((opaque.astype(float) - np.array(bg_rgb)) ** 2, axis=1))
    close_pct = float((diffs < 50).sum() / len(opaque))

    if close_pct > 0.3:
        return {"blending_risk": "HIGH", "pct_similar": round(close_pct * 100, 1)}
    elif close_pct > 0.1:
        return {"blending_risk": "MEDIUM", "pct_similar": round(close_pct * 100, 1)}
    else:
        return {"blending_risk": "LOW", "pct_similar": round(close_pct * 100, 1)}


# ─── BRAND CATEGORIES ──────────────────────────────────────────────────────

CATEGORIES = {
    "Food & Dining":          ["food", "restaurant", "cafe", "coffee", "pizza", "burger", "kitchen",
                               "bakery", "eat", "cook", "meal", "dine", "swiggy", "zomato", "domino",
                               "kfc", "mcdonald", "starbucks", "dunkin", "biryani", "chai", "tea"],
    "Shopping & Fashion":     ["fashion", "cloth", "wear", "apparel", "shoe", "footwear", "style",
                               "boutique", "jewel", "accessori", "shop", "store", "retail", "mart",
                               "myntra", "ajio", "nykaa", "flipkart", "amazon"],
    "Travel & Hotels":        ["travel", "hotel", "flight", "airline", "booking", "trip", "tour",
                               "resort", "hostel", "stay", "makemytrip", "goibibo", "oyo", "airbnb",
                               "airport", "lounge", "concierge"],
    "Entertainment":          ["stream", "music", "movie", "game", "gaming", "play", "netflix",
                               "spotify", "disney", "prime", "video", "media", "entertain", "ticket",
                               "event", "comic", "anime"],
    "Health & Wellness":      ["health", "fitness", "gym", "yoga", "pharma", "medic", "doctor",
                               "wellness", "supplement", "vitamin", "cure", "hospital", "clinic",
                               "ayurved", "organic", "diet", "nutri"],
    "Beauty & Personal Care": ["beauty", "cosmetic", "skin", "hair", "groom", "salon", "spa",
                               "makeup", "fragrance", "perfume", "derma", "care"],
    "Electronics & Tech":     ["tech", "electron", "gadget", "phone", "laptop", "computer", "software",
                               "app", "saas", "digital", "cloud", "data", "cyber", "kaspersky",
                               "norton", "samsung", "apple", "oneplus", "mixpanel", "mailchimp"],
    "Home & Living":          ["home", "furniture", "decor", "kitchen", "mattress", "bed", "bath",
                               "clean", "garden", "interior", "living", "house", "appliance"],
    "Education & Learning":   ["edu", "learn", "course", "school", "university", "training", "tutor",
                               "academy", "skill", "book", "library", "exam", "certif"],
    "Finance & Insurance":    ["finance", "bank", "insur", "invest", "loan", "credit", "pay",
                               "wallet", "money", "fund", "stock", "trading", "mutual", "fintech"],
    "Grocery & Essentials":   ["grocery", "supermarket", "grofer", "bigbasket", "blinkit", "fresh",
                               "vegetable", "fruit", "daily", "essential", "provision"],
    "Auto & Transport":       ["auto", "car", "bike", "fuel", "petrol", "diesel", "ev", "electric",
                               "transport", "ride", "cab", "taxi", "uber", "ola", "rapido", "tyre"],
}


def auto_categorize(brand_name: str, website_url: str, scraped_text: str = "") -> str:
    """Auto-categorize a brand using keyword matching. Returns best-guess category."""
    text = f"{brand_name} {website_url} {scraped_text}".lower()

    scores = {}
    for category, keywords in CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score

    if scores:
        return max(scores, key=scores.get)
    return "Uncategorized"


# ─── WEBSITE TEXT SCRAPING ─────────────────────────────────────────────────

def scrape_website_text(website_url: str) -> dict:
    """
    Scrape homepage + about page for brand description text.
    Returns {"homepage_text": str, "about_text": str, "meta_description": str}.
    """
    result = {"homepage_text": "", "about_text": "", "meta_description": ""}
    if not website_url:
        return result

    try:
        resp = requests.get(website_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return result
        soup = BeautifulSoup(resp.text, "html.parser")

        # Meta description (usually the best one-liner)
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            result["meta_description"] = meta["content"].strip()[:500]

        # Homepage text — get visible text from key areas
        for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body_text = soup.get_text(separator=" ", strip=True)
        # Take first 1000 chars of meaningful text
        result["homepage_text"] = re.sub(r'\s+', ' ', body_text)[:1000]

        # Try /about page
        base = resp.url.rstrip("/")
        for about_path in ["/about", "/about-us", "/company"]:
            try:
                aresp = requests.get(base + about_path, headers=HEADERS, timeout=8)
                if aresp.status_code == 200:
                    asoup = BeautifulSoup(aresp.text, "html.parser")
                    for tag in asoup.find_all(["script", "style", "nav", "footer"]):
                        tag.decompose()
                    about_text = asoup.get_text(separator=" ", strip=True)
                    result["about_text"] = re.sub(r'\s+', ' ', about_text)[:1000]
                    break
            except:
                continue
    except:
        pass

    return result


# ─── LOGO VALIDATION HEURISTICS ───────────────────────────────────────────

def validate_logo(img: Image.Image, brand_name: str) -> dict:
    """
    Heuristic checks to see if the fetched image is likely a real logo.
    Returns {"is_likely_logo": bool, "issues": [str], "score": float 0-1}.
    """
    issues = []
    score = 1.0
    w, h = img.size

    # Check 1: Aspect ratio — logos are usually roughly square or landscape
    # Tall/narrow images (like certification badges) are suspicious
    aspect = w / max(h, 1)
    if aspect < 0.3 or aspect > 4.0:
        issues.append(f"unusual aspect ratio ({aspect:.1f})")
        score *= 0.5

    # Check 2: Too many colours = probably a photo, not a logo
    try:
        small = img.convert("RGBA").resize((50, 50), Image.LANCZOS)
        arr = np.array(small)
        opaque = arr[arr[:, :, 3] > 200][:, :3]
        if len(opaque) > 100:
            unique_approx = len(set(tuple(c // 32) for c in opaque))  # quantize
            if unique_approx > 40:
                issues.append(f"too many colours ({unique_approx} clusters) — may be a photo")
                score *= 0.6
    except:
        pass

    # Check 3: Very small images are likely favicons, not real logos
    if w < 100 or h < 100:
        issues.append(f"very small ({w}x{h})")
        score *= 0.7

    # Check 4: Image is mostly one solid colour with a small element
    # (likely a banner or background, not a logo)
    try:
        rgba = np.array(img.convert("RGBA"))
        opaque = rgba[rgba[:, :, 3] > 200][:, :3]
        if len(opaque) > 0:
            most_common = np.median(opaque, axis=0)
            close = np.sqrt(np.sum((opaque.astype(float) - most_common) ** 2, axis=1)) < 30
            pct_uniform = float(close.sum() / len(opaque))
            if pct_uniform > 0.85:
                issues.append(f"{pct_uniform:.0%} of pixels are one colour — may be a banner")
                score *= 0.7
    except:
        pass

    return {
        "is_likely_logo": score > 0.5 and len(issues) <= 1,
        "issues": issues,
        "logo_quality_score": round(score, 2),
    }


# ─── HTML REVIEW PAGE ──────────────────────────────────────────────────────

def generate_review_html(results: list, out_dir: Path) -> Path | None:
    """Generate interactive HTML review page with colour override, mark-as-final, categories."""
    successful = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]
    if not successful and not failed:
        return None

    # Collect all categories for the filter bar
    all_categories = sorted(set(r.get("category", "Uncategorized") for r in successful))

    # Build brand data for JS
    brands_js = []
    for r in successful:
        safe_name = re.sub(r'[^a-z0-9_]', '_', r["brand_name"].lower()).strip("_")
        logo_path = out_dir / safe_name / "logo.png"
        logo_b64 = ""
        if logo_path.exists():
            logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()

        svg_path = out_dir / safe_name / "logo.svg"
        has_svg = svg_path.exists()
        svg_markup = ""
        if has_svg:
            try:
                raw = svg_path.read_text(errors="replace")
                # Only embed if it's a real SVG and not excessively large
                if "<svg" in raw.lower() and len(raw) < 200_000:
                    svg_markup = raw
            except Exception:
                pass

        brands_js.append({
            "name": r["brand_name"],
            "folder": safe_name,
            "website": r.get("website", ""),
            "colour": r.get("brand_colour") or "#333333",
            "colour_candidates": r.get("colour_candidates", []),
            "tier": r.get("source_tier"),
            "source": r.get("logo_source", ""),
            "confidence": r.get("confidence", 0),
            "blending_risk": r.get("blending_risk", "unknown"),
            "has_transparency": r.get("has_transparency", False),
            "bg_removed": r.get("bg_removed", False),
            "is_svg": r.get("is_svg", False),
            "has_svg_file": has_svg,
            "svg_markup": svg_markup,
            "undersize": r.get("undersize", False),
            "original_size": r.get("original_size", ""),
            "category": r.get("category", "Uncategorized"),
            "meta_description": r.get("meta_description", ""),
            "logo_quality_score": r.get("logo_quality_score", 1.0),
            "logo_issues": r.get("logo_issues", []),
            "logo_b64": logo_b64,
        })

    brands_json = json.dumps(brands_js, cls=SafeEncoder)
    failed_json = json.dumps([{"name": r["brand_name"], "errors": r.get("errors", [])} for r in failed])
    categories_json = json.dumps(all_categories)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brand Asset Pipeline — Review</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f5f5; color:#222; }}

  .toolbar {{
    position:sticky; top:0; z-index:100;
    background:#1a1a2e; color:#fff; padding:12px 24px;
    display:flex; align-items:center; gap:20px; flex-wrap:wrap;
    box-shadow:0 2px 12px rgba(0,0,0,0.15);
  }}
  .toolbar h1 {{ font-size:18px; font-weight:600; white-space:nowrap; }}
  .toolbar label {{ font-size:12px; opacity:0.7; }}
  .toolbar select, .toolbar input[type=range] {{ cursor:pointer; }}
  .control-group {{ display:flex; flex-direction:column; gap:3px; }}
  .control-group select {{ padding:4px 8px; border-radius:6px; border:1px solid #444; background:#2a2a4a; color:#fff; font-size:12px; }}
  .size-val {{ font-size:12px; font-variant-numeric:tabular-nums; min-width:36px; text-align:center; }}
  .stats {{ margin-left:auto; font-size:12px; opacity:0.8; display:flex; gap:14px; }}
  .stats span {{ white-space:nowrap; }}
  .progress-bar {{ width:120px; height:6px; background:#333; border-radius:3px; overflow:hidden; }}
  .progress-fill {{ height:100%; background:#4caf50; transition:width 0.3s; }}

  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--card-w,200px),1fr)); gap:16px; padding:24px; }}
  .card {{ background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,0.08); transition:box-shadow 0.2s; }}
  .card:hover {{ box-shadow:0 4px 16px rgba(0,0,0,0.12); }}
  .card.flagged {{ outline:3px solid #ff4444; }}
  .card.finalized {{ outline:3px solid #4caf50; }}
  .card-preview {{ aspect-ratio:1; display:flex; align-items:center; justify-content:center; overflow:hidden; }}
  .card-preview.shape-circle {{  }}
  .card-preview.shape-card {{ aspect-ratio:14/9; }}
  .card-preview img {{ width:60%; height:60%; object-fit:contain; transition:all 0.2s; }}
  .shape-circle img {{ border-radius:50%; }}
  .card-meta {{ padding:10px 12px; font-size:11px; line-height:1.6; border-top:1px solid #f0f0f0; }}
  .card-meta .brand-name {{ font-weight:600; font-size:13px; margin-bottom:2px; }}
  .card-meta .meta-row {{ display:flex; justify-content:space-between; color:#666; }}
  .card-meta .description {{ font-size:10px; color:#888; margin-top:4px; max-height:32px; overflow:hidden; }}
  .category-tag {{ display:inline-block; font-size:9px; padding:1px 6px; border-radius:10px; background:#eef; color:#336; margin-right:4px; }}
  .badge {{ display:inline-block; font-size:9px; padding:2px 5px; border-radius:4px; font-weight:600; text-transform:uppercase; }}
  .badge-low {{ background:#e6f9ed; color:#1b7a3d; }}
  .badge-medium {{ background:#fff3e0; color:#e65100; }}
  .badge-high {{ background:#fde8e8; color:#c0392b; }}
  .badge-svg {{ background:#e8f0fe; color:#1a73e8; }}
  .badge-rembg {{ background:#f3e8fd; color:#7b1fa2; }}
  .badge-undersize {{ background:#fff9c4; color:#f57f17; }}
  .badge-logoissue {{ background:#fce4ec; color:#c62828; }}
  .colour-options {{ display:flex; gap:3px; margin-top:5px; flex-wrap:wrap; align-items:center; }}
  .colour-options span.label {{ font-size:9px; color:#999; }}
  .colour-swatch {{ width:20px; height:20px; border-radius:5px; cursor:pointer; border:2px solid transparent; transition:all 0.15s; position:relative; }}
  .colour-swatch:hover {{ transform:scale(1.15); }}
  .colour-swatch.active {{ border-color:#222; box-shadow:0 0 0 1px #fff, 0 0 0 3px #222; }}
  .colour-input {{ width:20px; height:20px; border:1px solid #ccc; border-radius:5px; cursor:pointer; padding:0; }}
  .risk-dot {{ width:5px; height:5px; border-radius:50%; position:absolute; top:1px; right:1px; }}
  .risk-dot.r-low {{ background:#28a745; }} .risk-dot.r-medium {{ background:#ff9800; }} .risk-dot.r-high {{ background:#dc3545; }}
  .card-actions {{ display:flex; gap:4px; margin-top:6px; }}
  .btn-final {{ flex:1; padding:4px 8px; border:1px solid #4caf50; border-radius:6px; background:#fff; color:#4caf50; font-size:10px; font-weight:600; cursor:pointer; transition:all 0.15s; }}
  .btn-final:hover {{ background:#e8f5e9; }}
  .btn-final.done {{ background:#4caf50; color:#fff; }}
  .btn-flag {{ padding:4px 8px; border:1px solid #ff5722; border-radius:6px; background:#fff; color:#ff5722; font-size:10px; cursor:pointer; }}
  .btn-flag:hover {{ background:#fbe9e7; }}
  .btn-flag.active {{ background:#ff5722; color:#fff; }}
  .btn-recolour {{ padding:4px 8px; border:1px solid #1a73e8; border-radius:6px; background:#fff; color:#1a73e8; font-size:10px; cursor:pointer; }}
  .btn-recolour:hover {{ background:#e8f0fe; }}
  .btn-recolour.active {{ background:#1a73e8; color:#fff; }}
  .recolour-row {{ display:none; margin-top:4px; align-items:center; gap:6px; }}
  .recolour-row.visible {{ display:flex; }}
  .recolour-row label {{ font-size:9px; color:#666; }}
  .recolour-input {{ width:24px; height:24px; border:1px solid #ccc; border-radius:5px; cursor:pointer; padding:0; }}
  .website-link {{ font-size:10px; color:#1a73e8; text-decoration:none; word-break:break-all; display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .website-link:hover {{ text-decoration:underline; }}
  .card-preview .svg-overlay {{ width:60%; height:60%; display:flex; align-items:center; justify-content:center; }}
  .card-preview .svg-overlay svg {{ width:100%; height:100%; }}
  .failed-section {{ padding:0 24px 24px; }}
  .failed-section h2 {{ font-size:14px; color:#888; margin-bottom:8px; }}
  .failed-item {{ font-size:12px; color:#999; padding:2px 0; }}
  .filter-bar {{ padding:6px 24px; background:#fff; border-bottom:1px solid #eee; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  .filter-btn {{ padding:3px 10px; border:1px solid #ddd; border-radius:16px; background:#fff; font-size:11px; cursor:pointer; transition:all 0.15s; }}
  .filter-btn:hover {{ background:#f0f0f0; }}
  .filter-btn.active {{ background:#1a1a2e; color:#fff; border-color:#1a1a2e; }}
  .export-bar {{ padding:12px 24px; background:#fff; border-top:1px solid #eee; position:sticky; bottom:0; display:flex; align-items:center; gap:16px; z-index:50; }}
  .btn-export {{ padding:8px 20px; background:#1a1a2e; color:#fff; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }}
  .btn-export:hover {{ background:#2a2a4e; }}
</style>
</head>
<body>
<div class="toolbar">
  <h1>Brand Assets Review</h1>
  <div class="control-group"><label>Shape</label>
    <select id="shapeSelect"><option value="square">Square</option><option value="circle">Circle</option><option value="card">Card (14:9)</option></select>
  </div>
  <div class="control-group"><label>Size</label>
    <div style="display:flex;align-items:center;gap:6px;"><input type="range" id="sizeSlider" min="120" max="400" value="220" step="20"><span class="size-val" id="sizeVal">220px</span></div>
  </div>
  <div class="control-group"><label>Sort</label>
    <select id="sortSelect"><option value="name">Name</option><option value="confidence">Confidence</option><option value="risk">Blending Risk</option><option value="tier">Source Tier</option><option value="category">Category</option></select>
  </div>
  <div class="control-group"><label>Category</label>
    <select id="categorySelect"><option value="all">All categories</option></select>
  </div>
  <div class="stats">
    <span id="statTotal"></span>
    <span id="statFinal" style="color:#4caf50;">Finalized: 0</span>
    <span id="statFlagged" style="color:#ff5722;">Flagged: 0</span>
    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
  </div>
</div>

<div class="filter-bar" id="filterBar">
  <span style="font-size:11px;color:#888;">Filter:</span>
  <button class="filter-btn active" data-filter="all">All</button>
  <button class="filter-btn" data-filter="finalized">Finalized</button>
  <button class="filter-btn" data-filter="pending">Pending</button>
  <button class="filter-btn" data-filter="flagged">Flagged</button>
  <button class="filter-btn" data-filter="low">Low risk</button>
  <button class="filter-btn" data-filter="high">High risk</button>
  <button class="filter-btn" data-filter="svg">SVG</button>
  <button class="filter-btn" data-filter="logoissue">Logo issues</button>
</div>

<div class="grid" id="grid"></div>
<div class="failed-section" id="failedSection"></div>

<div class="export-bar">
  <button class="btn-export" onclick="exportFinal()">Export finalized brands (JSON)</button>
  <button class="btn-export" style="background:#555;" onclick="exportCSV()">Export as CSV</button>
  <span id="exportStatus" style="font-size:12px;color:#666;"></span>
</div>

<script>
const BRANDS = {brands_json};
const FAILED = {failed_json};
const CATEGORIES = {categories_json};

let finalized = new Set();
let flagged = new Set();
let selectedColours = {{}};
let logoRecolours = {{}};  // folder -> hex or null
let activeFilter = "all";
let activeCategory = "all";
BRANDS.forEach(b => {{ selectedColours[b.folder] = b.colour; }});

// Populate category dropdown
const catSel = document.getElementById("categorySelect");
CATEGORIES.forEach(c => {{ const o = document.createElement("option"); o.value = c; o.textContent = c; catSel.appendChild(o); }});

function getActiveColour(b) {{ return selectedColours[b.folder] || b.colour; }}

function switchColour(folder, hex) {{
  selectedColours[folder] = hex;
  const card = document.querySelector(`.card[data-folder="${{folder}}"]`);
  if (!card) return;
  card.querySelector(".card-preview").style.background = hex;
  card.querySelectorAll(".colour-swatch").forEach(sw => sw.classList.toggle("active", sw.dataset.hex === hex));
  const hexSpan = card.querySelector(".hex-display");
  if (hexSpan) hexSpan.textContent = hex;
}}

function customColour(folder, inputEl) {{
  switchColour(folder, inputEl.value);
}}

function recolourSVG(folder, hex) {{
  logoRecolours[folder] = hex;
  const card = document.querySelector(`.card[data-folder="${{folder}}"]`);
  if (!card) return;
  const overlay = card.querySelector(".svg-overlay");
  if (!overlay) return;
  const b = BRANDS.find(x => x.folder === folder);
  if (!b || !b.svg_markup) return;
  // Replace fill and stroke colours in the SVG markup
  let svg = b.svg_markup;
  // Replace fill="..." but not fill="none" or fill="url(...)"
  svg = svg.replace(/fill="(?!none|url)([^"]*)"/gi, `fill="${{hex}}"`);
  svg = svg.replace(/stroke="(?!none|url)([^"]*)"/gi, `stroke="${{hex}}"`);
  // Also handle style="fill:..." inline
  svg = svg.replace(/fill:\s*(?!none|url)[^;"]+/gi, `fill:${{hex}}`);
  svg = svg.replace(/stroke:\s*(?!none|url)[^;"]+/gi, `stroke:${{hex}}`);
  overlay.innerHTML = svg;
  overlay.style.display = "flex";
  // Hide the PNG img
  const img = card.querySelector(".card-preview img");
  if (img) img.style.display = "none";
}}

function resetLogoColour(folder) {{
  delete logoRecolours[folder];
  const card = document.querySelector(`.card[data-folder="${{folder}}"]`);
  if (!card) return;
  const overlay = card.querySelector(".svg-overlay");
  if (overlay) {{ overlay.innerHTML = ""; overlay.style.display = "none"; }}
  const img = card.querySelector(".card-preview img");
  if (img) img.style.display = "";
}}

function toggleRecolourRow(folder) {{
  const card = document.querySelector(`.card[data-folder="${{folder}}"]`);
  if (!card) return;
  const row = card.querySelector(".recolour-row");
  if (row) row.classList.toggle("visible");
}}

function toggleFinal(folder) {{
  if (finalized.has(folder)) finalized.delete(folder); else finalized.add(folder);
  renderGrid();
}}

function toggleFlag(folder) {{
  if (flagged.has(folder)) flagged.delete(folder); else flagged.add(folder);
  renderGrid();
}}

function applyFilters(brands) {{
  if (activeFilter === "finalized") brands = brands.filter(b => finalized.has(b.folder));
  else if (activeFilter === "pending") brands = brands.filter(b => !finalized.has(b.folder));
  else if (activeFilter === "flagged") brands = brands.filter(b => flagged.has(b.folder));
  else if (activeFilter === "low") brands = brands.filter(b => b.blending_risk === "LOW");
  else if (activeFilter === "high") brands = brands.filter(b => b.blending_risk === "HIGH");
  else if (activeFilter === "svg") brands = brands.filter(b => b.is_svg || b.has_svg_file);
  else if (activeFilter === "logoissue") brands = brands.filter(b => b.logo_issues && b.logo_issues.length > 0);
  if (activeCategory !== "all") brands = brands.filter(b => b.category === activeCategory);
  return brands;
}}

function renderGrid() {{
  const grid = document.getElementById("grid");
  const shape = document.getElementById("shapeSelect").value;
  const size = parseInt(document.getElementById("sizeSlider").value);
  const sort = document.getElementById("sortSelect").value;
  document.getElementById("sizeVal").textContent = size + "px";
  grid.style.setProperty("--card-w", size + "px");

  let brands = applyFilters([...BRANDS]);

  if (sort === "confidence") brands.sort((a,b) => a.confidence - b.confidence);
  else if (sort === "risk") {{ const ro = {{"HIGH":0,"MEDIUM":1,"LOW":2,"unknown":3}}; brands.sort((a,b) => (ro[a.blending_risk]||3) - (ro[b.blending_risk]||3)); }}
  else if (sort === "tier") brands.sort((a,b) => (a.tier||9) - (b.tier||9));
  else if (sort === "category") brands.sort((a,b) => a.category.localeCompare(b.category));
  else brands.sort((a,b) => a.name.localeCompare(b.name));

  let html = "";
  for (const b of brands) {{
    const isFinal = finalized.has(b.folder);
    const isFlagged = flagged.has(b.folder);
    const shapeClass = shape === "circle" ? "shape-circle" : shape === "card" ? "shape-card" : "";
    const ac = getActiveColour(b);
    const rc = b.blending_risk === "LOW" ? "badge-low" : b.blending_risk === "MEDIUM" ? "badge-medium" : "badge-high";

    let badges = `<span class="badge ${{rc}}">${{b.blending_risk}}</span> `;
    if (b.is_svg || b.has_svg_file) badges += `<span class="badge badge-svg">SVG</span> `;
    if (b.bg_removed) badges += `<span class="badge badge-rembg">BG Rem</span> `;
    if (b.undersize) badges += `<span class="badge badge-undersize">Upscaled</span> `;
    if (b.logo_issues && b.logo_issues.length) badges += `<span class="badge badge-logoissue" title="${{b.logo_issues.join(', ')}}">!</span> `;

    let swatches = `<span class="label">BG:</span>`;
    const cands = b.colour_candidates || [{{hex:b.colour, source:"auto", blending_risk:b.blending_risk}}];
    for (const c of cands) {{
      const isAct = c.hex === ac;
      const rd = c.blending_risk === "LOW" ? "r-low" : c.blending_risk === "MEDIUM" ? "r-medium" : c.blending_risk === "HIGH" ? "r-high" : "";
      const bdr = c.hex === "#FFFFFF" ? "border:1px solid #ddd;" : "";
      swatches += `<div class="colour-swatch ${{isAct?"active":""}}" style="background:${{c.hex}};${{bdr}}" data-hex="${{c.hex}}" title="${{c.hex}} (${{c.source}})" onclick="switchColour('${{b.folder}}','${{c.hex}}')">${{rd?`<span class="risk-dot ${{rd}}"></span>`:""}}</div>`;
    }}
    swatches += `<input type="color" class="colour-input" value="${{ac}}" onchange="customColour('${{b.folder}}',this)" title="Custom colour">`;

    const desc = b.meta_description ? `<div class="description">${{b.meta_description}}</div>` : "";
    const websiteLink = b.website ? `<a class="website-link" href="${{b.website}}" target="_blank" rel="noopener">${{b.website.replace(/^https?:\\/\\//, "")}}</a>` : "";
    const hasSvgMarkup = b.svg_markup && b.svg_markup.length > 0;
    const recolourBtn = hasSvgMarkup ? `<button class="btn-recolour" onclick="toggleRecolourRow('${{b.folder}}')">Recolour Logo</button>` : "";
    const currentRecolour = logoRecolours[b.folder] || "#FFFFFF";
    const recolourRow = hasSvgMarkup ? `<div class="recolour-row"><label>Logo colour:</label><input type="color" class="recolour-input" value="${{currentRecolour}}" onchange="recolourSVG('${{b.folder}}',this.value)"><button style="font-size:9px;padding:2px 6px;border:1px solid #ccc;border-radius:4px;background:#fff;cursor:pointer;" onclick="resetLogoColour('${{b.folder}}')">Reset</button></div>` : "";
    // If SVG was recoloured, show the recoloured SVG overlay
    const svgOverlayContent = (hasSvgMarkup && logoRecolours[b.folder]) ? (() => {{
      let sv = b.svg_markup;
      const rh = logoRecolours[b.folder];
      sv = sv.replace(/fill="(?!none|url)([^"]*)"/gi, `fill="${{rh}}"`);
      sv = sv.replace(/stroke="(?!none|url)([^"]*)"/gi, `stroke="${{rh}}"`);
      sv = sv.replace(/fill:\\s*(?!none|url)[^;"]+/gi, `fill:${{rh}}`);
      sv = sv.replace(/stroke:\\s*(?!none|url)[^;"]+/gi, `stroke:${{rh}}`);
      return sv;
    }})() : "";
    const imgDisplay = svgOverlayContent ? "none" : "";
    const svgDisplay = svgOverlayContent ? "flex" : "none";

    html += `
      <div class="card ${{isFinal?"finalized":""}} ${{isFlagged?"flagged":""}}" data-folder="${{b.folder}}">
        <div class="card-preview ${{shapeClass}}" style="background:${{ac}}">
          <img src="data:image/png;base64,${{b.logo_b64}}" alt="${{b.name}}" loading="lazy" style="${{imgDisplay ? "display:none" : ""}}">
          <div class="svg-overlay" style="display:${{svgDisplay}}">${{svgOverlayContent}}</div>
        </div>
        <div class="card-meta">
          <div class="brand-name">${{b.name}}</div>
          ${{websiteLink}}
          <span class="category-tag">${{b.category}}</span>
          <div class="meta-row"><span class="hex-display">${{ac}}</span><span>T${{b.tier}} ${{Math.round(b.confidence*100)}}%</span></div>
          <div style="margin-top:3px">${{badges}}</div>
          ${{desc}}
          <div class="colour-options">${{swatches}}</div>
          ${{recolourRow}}
          <div class="card-actions">
            <button class="btn-final ${{isFinal?"done":""}}" onclick="toggleFinal('${{b.folder}}')">${{isFinal?"Finalized":"Mark Final"}}</button>
            <button class="btn-flag ${{isFlagged?"active":""}}" onclick="toggleFlag('${{b.folder}}')">${{isFlagged?"Unflag":"Flag"}}</button>
            ${{recolourBtn}}
          </div>
        </div>
      </div>`;
  }}
  grid.innerHTML = html;

  const finalCount = finalized.size;
  document.getElementById("statTotal").textContent = `${{brands.length}} / ${{BRANDS.length}}`;
  document.getElementById("statFinal").textContent = `Finalized: ${{finalCount}}`;
  document.getElementById("statFlagged").textContent = `Flagged: ${{flagged.size}}`;
  document.getElementById("progressFill").style.width = `${{Math.round(finalCount/BRANDS.length*100)}}%`;
}}

function exportFinal() {{
  const data = BRANDS.filter(b => finalized.has(b.folder)).map(b => ({{
    brand_name: b.name,
    folder: b.folder,
    category: b.category,
    website: b.website || "",
    bg_colour: selectedColours[b.folder] || b.colour,
    logo_recolour: logoRecolours[b.folder] || null,
    meta_description: b.meta_description || "",
    logo_file: b.folder + "/logo.png",
    svg_file: b.has_svg_file ? b.folder + "/logo.svg" : null,
    is_svg: b.is_svg,
    source_tier: b.tier,
    logo_source: b.source,
    confidence: b.confidence,
    blending_risk: b.blending_risk,
    logo_quality_score: b.logo_quality_score,
    logo_issues: b.logo_issues || [],
    original_size: b.original_size,
    undersize: b.undersize,
    bg_removed: b.bg_removed,
  }}));
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: "application/json"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "approved_brands.json"; a.click();
  document.getElementById("exportStatus").textContent = `Exported ${{data.length}} brands`;
}}

function exportCSV() {{
  const data = BRANDS.filter(b => finalized.has(b.folder)).map(b => ({{
    brand_name: b.name,
    category: b.category,
    website: b.website || "",
    bg_colour: selectedColours[b.folder] || b.colour,
    logo_recolour: logoRecolours[b.folder] || "",
    meta_description: b.meta_description || "",
    logo_file: b.folder + "/logo.png",
    svg_file: b.has_svg_file ? b.folder + "/logo.svg" : "",
    is_svg: b.is_svg,
    source_tier: b.tier,
    logo_source: b.source,
    confidence: b.confidence,
    blending_risk: b.blending_risk,
    logo_quality_score: b.logo_quality_score,
    original_size: b.original_size,
  }}));
  if (!data.length) {{ document.getElementById("exportStatus").textContent = "No finalized brands to export"; return; }}
  const headers = Object.keys(data[0]);
  const csv = [headers.join(","), ...data.map(r => headers.map(h => `"${{String(r[h]||"").replace(/"/g,'""')}}"`).join(","))].join("\\n");
  const blob = new Blob([csv], {{type:"text/csv"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "approved_brands.csv"; a.click();
  document.getElementById("exportStatus").textContent = `Exported ${{data.length}} brands as CSV`;
}}

document.getElementById("shapeSelect").addEventListener("change", renderGrid);
document.getElementById("sizeSlider").addEventListener("input", renderGrid);
document.getElementById("sortSelect").addEventListener("change", renderGrid);
document.getElementById("categorySelect").addEventListener("change", e => {{ activeCategory = e.target.value; renderGrid(); }});
document.querySelectorAll(".filter-btn").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeFilter = btn.dataset.filter;
    renderGrid();
  }});
}});

if (FAILED.length) {{
  let fhtml = `<h2>Failed (${{FAILED.length}})</h2>`;
  for (const f of FAILED) fhtml += `<div class="failed-item">${{f.name}}: ${{f.errors.join(", ")||"unknown"}}</div>`;
  document.getElementById("failedSection").innerHTML = fhtml;
}}

renderGrid();
</script>
</body>
</html>"""

    out_path = out_dir / "review.html"
    out_path.write_text(html)
    return out_path


# ─── MAIN PIPELINE ───────────────────────────────────────────────────────────

def process_brand(brand_name: str, website: str,
                   existing_logo_url: str = "", existing_color: str = "") -> dict:
    """Run the full pipeline for a single brand."""
    result = {
        "brand_name": brand_name,
        "website": website,
        "status": "failed",
        "source_tier": None,
        "logo_source": None,
        "logo_url": None,
        "is_svg": False,
        "has_transparency": False,
        "bg_removed": False,
        "undersize": False,
        "original_size": "",
        "brand_colour": None,
        "colour_source": None,
        "colour_candidates": [],
        "blending_risk": None,
        "confidence": 0,
        "category": "Uncategorized",
        "meta_description": "",
        "scraped_text": "",
        "logo_quality_score": 1.0,
        "logo_issues": [],
        "errors": [],
    }

    # ── TIER 0: Use pre-existing logo URL from CSV ───────────────────────────
    logo_data = None
    if existing_logo_url:
        img, is_svg, svg_raw = _fetch_image(existing_logo_url)
        if img:
            logo_data = {
                "source": "csv:provided",
                "image": img, "is_svg": is_svg,
                "url": existing_logo_url, "confidence": 0.95,
            }
            if svg_raw:
                logo_data["svg_data"] = svg_raw
            result["source_tier"] = 0

    # ── TIER 1: Brandfetch (domain CDN + search) ─────────────────────────────
    if not logo_data:
        logo_data = tier1_brandfetch(brand_name, website)
        if logo_data:
            result["source_tier"] = 1

    # ── TIER 2: Website scraping ──────────────────────────────────────────────
    theme_color = None
    if not logo_data:
        logo_data = tier2_website_scrape(website)
        if logo_data:
            result["source_tier"] = 2
            theme_color = logo_data.get("theme_color")

    # ── TIER 3: Wikimedia Commons + Wikipedia ────────────────────────────────
    if not logo_data:
        logo_data = tier3_wikimedia(brand_name)
        if logo_data:
            result["source_tier"] = 3

    # ── TIER 4: Google Favicon ────────────────────────────────────────────────
    if not logo_data:
        logo_data = tier4_google_favicon(website)
        if logo_data:
            result["source_tier"] = 4

    # ── TIER 5: DuckDuckGo ────────────────────────────────────────────────────
    if not logo_data:
        logo_data = tier5_duckduckgo(brand_name)
        if logo_data:
            result["source_tier"] = 5

    # ── TIER 6: Gilbarbara SVG repo ───────────────────────────────────────────
    if not logo_data:
        logo_data = tier6_gilbarbara(brand_name)
        if logo_data:
            result["source_tier"] = 6

    # ── TIER 7: Simple Icons ─────────────────────────────────────────────────
    if not logo_data:
        logo_data = tier7_simple_icons(brand_name)
        if logo_data:
            result["source_tier"] = 7

    if not logo_data:
        result["errors"].append("No logo found in any tier")
        return result

    # ── Record source info ───────────────────────────────────────────────────
    raw_img = logo_data["image"]
    result["logo_source"] = logo_data["source"]
    result["logo_url"] = logo_data.get("url")
    result["is_svg"] = logo_data.get("is_svg", False)
    result["has_transparency"] = bool(has_transparency(raw_img))
    result["original_size"] = f"{raw_img.width}x{raw_img.height}"

    # ── Size check ───────────────────────────────────────────────────────────
    if not result["is_svg"] and (raw_img.width < TARGET_SIZE or raw_img.height < TARGET_SIZE):
        result["undersize"] = True

    # ── Background removal (rembg) ───────────────────────────────────────────
    if not result["has_transparency"]:
        try:
            raw_img = remove_background(raw_img)
            result["bg_removed"] = True
        except Exception as e:
            result["errors"].append(f"rembg failed: {e}")
            result["bg_removed"] = False

    # ── Auto-crop transparent borders ────────────────────────────────────────
    if has_transparency(raw_img) or result["bg_removed"]:
        try:
            raw_img = auto_crop_transparent(raw_img)
        except Exception as e:
            result["errors"].append(f"auto-crop failed: {e}")

    processed = process_logo(raw_img)

    # ── Extract colour candidates ────────────────────────────────────────────
    if not theme_color and logo_data.get("theme_color"):
        theme_color = logo_data["theme_color"]
    # Use existing_color from CSV as theme_color if we have it
    if existing_color and not theme_color:
        theme_color = existing_color
    si_colour = logo_data.get("si_colour")  # from Simple Icons if available
    colours = extract_brand_colours(processed, theme_color, si_colour)
    result["brand_colour"] = colours["primary"]
    result["colour_source"] = colours["primary_source"]
    result["colour_candidates"] = colours["candidates"]  # list of {hex, source, blending_risk}

    # ── Blending risk (for the primary colour) ───────────────────────────────
    blend = validate_bg_colour(processed, colours["primary"])
    result["blending_risk"] = blend["blending_risk"]

    # ── Confidence ───────────────────────────────────────────────────────────
    conf = logo_data.get("confidence", 0.5)
    if not result["has_transparency"] and not result["bg_removed"]:
        conf *= 0.7
    if blend["blending_risk"] == "HIGH":
        conf *= 0.6
    if result["undersize"]:
        conf *= 0.8
    if result["is_svg"]:
        conf = min(conf * 1.1, 1.0)  # SVG bonus
    result["confidence"] = round(conf, 2)
    result["status"] = "success"

    # ── Logo validation ──────────────────────────────────────────────────────
    validation = validate_logo(processed, brand_name)
    result["logo_quality_score"] = validation["logo_quality_score"]
    result["logo_issues"] = validation["issues"]
    if not validation["is_likely_logo"]:
        result["confidence"] *= 0.5  # heavy penalty
        result["confidence"] = round(result["confidence"], 2)

    # ── Website text scraping ────────────────────────────────────────────────
    scraped = scrape_website_text(website)
    result["meta_description"] = scraped["meta_description"]
    result["scraped_text"] = scraped["homepage_text"][:500]  # keep it lean

    # ── Auto-categorize ──────────────────────────────────────────────────────
    result["category"] = auto_categorize(
        brand_name, website,
        f"{scraped['meta_description']} {scraped['homepage_text']}"
    )

    # ── Save outputs ─────────────────────────────────────────────────────────
    safe_name = re.sub(r'[^a-z0-9_]', '_', brand_name.lower()).strip("_")
    brand_dir = OUT_DIR / safe_name
    brand_dir.mkdir(parents=True, exist_ok=True)

    processed.save(brand_dir / "logo.png")
    logo_data["image"].save(brand_dir / "logo_raw.png")

    # Save SVG if we have it
    if logo_data.get("svg_data"):
        (brand_dir / "logo.svg").write_bytes(logo_data["svg_data"])

    with open(brand_dir / "meta.json", "w") as f:
        json.dump({k: v for k, v in result.items() if k != "image"}, f, indent=2, cls=SafeEncoder)

    return result


def main():
    parser = argparse.ArgumentParser(description="Brand Asset Pipeline PoC v2")
    parser.add_argument("--input", required=True, help="Input CSV file")
    parser.add_argument("--sample", type=int, default=0, help="Number of brands to sample (0=all)")
    parser.add_argument("--output", default="./brand_assets", help="Output directory")
    parser.add_argument("--rembg-model", default="u2net",
                        choices=["u2net", "u2net_human_seg", "isnet-general-use"],
                        help="Background removal model (default: u2net)")
    parser.add_argument("--alpha-matting", action="store_true",
                        help="Enable alpha matting for cleaner edges (slower)")
    args = parser.parse_args()

    global OUT_DIR, REMBG_MODEL, ALPHA_MATTING
    OUT_DIR = Path(args.output)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REMBG_MODEL = args.rembg_model
    ALPHA_MATTING = args.alpha_matting

    # Read CSV
    with open(args.input) as f:
        rows = list(csv.DictReader(f))

    if args.sample and args.sample < len(rows):
        import random
        random.seed(42)
        rows = random.sample(rows, args.sample)

    print(f"\n{'='*60}")
    print(f"  Brand Asset Pipeline v2 — Processing {len(rows)} brands")
    print(f"  Output:  {OUT_DIR.absolute()}")
    print(f"  Model:   {REMBG_MODEL}  |  Alpha matting: {'ON' if ALPHA_MATTING else 'OFF'}")
    svg_status = "\u2705 cairosvg installed" if HAS_CAIROSVG else "\u26a0\ufe0f  cairosvg NOT installed — SVGs will be saved but not rasterized"
    print(f"  SVG:     {svg_status}")
    print(f"{'='*60}\n")
    if not HAS_CAIROSVG:
        print("  TIP: Install cairosvg for SVG support:  brew install cairo && pip install cairosvg\n")

    results = []
    tier_counts = Counter()
    status_counts = Counter()

    # Auto-detect CSV column names
    if rows:
        cols = list(rows[0].keys())
        col_name = next((c for c in cols if c.lower() in ("brand_name", "name", "brand")), cols[0])
        col_site = next((c for c in cols if c.lower() in ("business_website", "url", "website", "site")), "")
        col_logo = next((c for c in cols if c.lower() in ("logo", "logo_url", "image", "image_url")), "")
        col_color = next((c for c in cols if c.lower() in ("color", "colour", "brand_colour", "hex")), "")
        print(f"  CSV columns: name={col_name}, site={col_site or 'N/A'}, logo={col_logo or 'N/A'}, color={col_color or 'N/A'}\n")

    for i, row in enumerate(rows):
        name = row.get(col_name, "").strip()
        site = row.get(col_site, "").strip() if col_site else ""
        existing_logo_url = row.get(col_logo, "").strip() if col_logo else ""
        existing_color = row.get(col_color, "").strip() if col_color else ""
        if not name:
            continue

        print(f"[{i+1:3d}/{len(rows)}] {name:35s} ", end="", flush=True)

        result = process_brand(name, site, existing_logo_url, existing_color)
        results.append(result)

        status_counts[result["status"]] += 1
        if result["source_tier"]:
            tier_counts[f"tier{result['source_tier']}"] += 1

        if result["status"] == "success":
            risk = result.get("blending_risk", "")
            emoji = "\u2705" if risk == "LOW" else ("\u26a0\ufe0f " if risk == "MEDIUM" else "\U0001f534")
            svg_tag = " [SVG]" if result.get("is_svg") else ""
            size_tag = " [UNDERSIZE]" if result.get("undersize") else ""
            print(f"{emoji}  T{result['source_tier']}  {result['brand_colour']}  conf={result['confidence']}{svg_tag}{size_tag}")
        else:
            print(f"\u274c  {result['errors'][:1]}")

        time.sleep(0.3)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Total:      {len(results)}")
    print(f"  Success:    {status_counts.get('success', 0)}")
    print(f"  Failed:     {status_counts.get('failed', 0)}")
    print(f"  Tier 0 (CSV provided):  {tier_counts.get('tier0', 0)}")
    print(f"  Tier 1 (Brandfetch):    {tier_counts.get('tier1', 0)}")
    print(f"  Tier 2 (Website):       {tier_counts.get('tier2', 0)}")
    print(f"  Tier 3 (Wikimedia):     {tier_counts.get('tier3', 0)}")
    print(f"  Tier 4 (Favicon):       {tier_counts.get('tier4', 0)}")
    print(f"  Tier 5 (DuckDuckGo):    {tier_counts.get('tier5', 0)}")
    print(f"  Tier 6 (Gilbarbara):    {tier_counts.get('tier6', 0)}")
    print(f"  Tier 7 (Simple Icons):  {tier_counts.get('tier7', 0)}")

    svg_count = sum(1 for r in results if r.get("is_svg"))
    undersize_count = sum(1 for r in results if r.get("undersize"))
    bg_removed = sum(1 for r in results if r.get("bg_removed"))
    high_risk = [r for r in results if r.get("blending_risk") == "HIGH"]

    print(f"\n  Quality indicators:")
    print(f"    SVG logos found:      {svg_count}")
    print(f"    Undersize (<500px):   {undersize_count}")
    print(f"    BG removed (rembg):   {bg_removed}")
    print(f"    High blending risk:   {len(high_risk)}")

    if high_risk:
        print(f"\n  Brands with HIGH blending risk:")
        for r in high_risk[:10]:
            print(f"    - {r['brand_name']}: {r['brand_colour']}")

    # Save summary JSON
    with open(OUT_DIR / "pipeline_summary.json", "w") as f:
        json.dump(results, f, indent=2, cls=SafeEncoder, default=str)

    # Save review CSV
    with open(OUT_DIR / "review.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "brand_name", "status", "source_tier", "logo_source", "is_svg",
            "brand_colour", "colour_source", "colour_candidates", "blending_risk",
            "has_transparency", "bg_removed", "undersize", "original_size",
            "confidence", "logo_url"
        ])
        w.writeheader()
        for r in results:
            row = {k: r.get(k) for k in w.fieldnames}
            # Stringify candidates list for CSV
            if isinstance(row.get("colour_candidates"), list):
                row["colour_candidates"] = " | ".join(
                    f"{c['hex']}({c.get('source','?')})" for c in row["colour_candidates"]
                )
            w.writerow(row)

    # ── Interactive HTML Review ──────────────────────────────────────────────
    print(f"\n  Generating interactive review page...")
    html_path = generate_review_html(results, OUT_DIR)
    if html_path:
        print(f"  \u2705 Review page: {html_path}")
    else:
        print(f"  \u26a0\ufe0f  No results — skipped review page")

    print(f"\n  \U0001f4c1 Assets saved to: {OUT_DIR.absolute()}")
    print(f"  \U0001f310 Review page:    open {OUT_DIR / 'review.html'}")
    print(f"  \U0001f4cb Review CSV:      {OUT_DIR / 'review.csv'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
