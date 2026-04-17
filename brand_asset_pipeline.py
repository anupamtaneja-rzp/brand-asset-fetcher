#!/usr/bin/env python3
"""
Brand Asset Pipeline — v4
===============================
Automatically sources brand logos (SVG preferred), removes backgrounds,
extracts brand colours, and generates an interactive HTML review page.

Sourcing tiers (in order):
    0. CSV-provided logo URL (if present in input)
    1. Brandfetch (logo.dev CDN + search API)
    2. Website scraping (apple-touch-icon, og:image, SVG, img-logo, favicon)
    3. Wikimedia Commons + Wikipedia (often has SVG logos for well-known brands)
    4. Google Favicon API (low quality, but wide coverage)
    5. DuckDuckGo Instant Answer (free, returns brand images)
    6. Gilbarbara SVG logo repo (2000+ brand SVGs on GitHub)
    7. Seeklogo.com (large vector logo collection)
    8. Simple Icons (3000+ brand SVGs + brand colours)

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

import argparse, csv, json, os, re, sys, time, hashlib, base64, concurrent.futures, logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
from io import BytesIO
from collections import Counter
from xml.etree import ElementTree as ET

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

# Check playwright availability once at startup
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ─── LOGGING ──────────────────────────────────────────────────────────────────
# Two loggers: 'pipeline' goes to pipeline.log (verbose), terminal stays clean.
log = logging.getLogger("pipeline")
log.setLevel(logging.DEBUG)
# File handler added in main() once we know the output directory.
# Console handler: only WARNING+ by default (overridable with --log-level debug).
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setLevel(logging.WARNING)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_console_handler)


def _setup_file_logging(out_dir: Path, console_level: str = "info"):
    """Attach file handler to the pipeline logger. Called once from main()."""
    fh = logging.FileHandler(out_dir / "pipeline.log", mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(fh)
    if console_level == "debug":
        _console_handler.setLevel(logging.DEBUG)


# ─── GLOBAL CONFIG (set by CLI args in main()) ────────────────────────────────
OUT_DIR = Path("./brand_assets")
REMBG_MODEL = "u2net"
ALPHA_MATTING = False
CANDIDATE_CAP = 50        # max candidates per brand
TARGET_SIZE = 500         # minimum output size (upscale if below)

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
MIN_LOGO_SIZE = 48        # lowered — we keep everything, even small favicons as candidates
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Extra headers specifically for image fetches (Shopify CDN etc.)
IMAGE_HEADERS = {
    **HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


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
        # Fix protocol-relative URLs
        if url.startswith("//"):
            url = "https:" + url
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            log.debug(f"[svg] HTTP {resp.status_code} fetching {url[:80]}")
            return None
        content = resp.content
        # Basic SVG validation
        text = content.decode("utf-8", errors="ignore")[:2000]
        if "<svg" in text.lower():
            return content
        log.debug(f"[svg] No <svg> tag in response from {url[:80]}")
        return None
    except Exception as e:
        log.debug(f"[svg] Exception fetching {url[:80]}: {e}")
        return None


def _svg_to_pil(svg_bytes: bytes, size: int = 500) -> Image.Image | None:
    """Rasterise SVG to PIL Image at given size. Requires cairosvg."""
    if not HAS_CAIROSVG:
        return None
    try:
        png_data = cairosvg.svg2png(bytestring=svg_bytes, output_width=size, output_height=size)
        return Image.open(BytesIO(png_data)).convert("RGBA")
    except Exception as e:
        log.debug(f"[svg] cairosvg failed: {e}")
        return None


def make_svg_square(svg_bytes: bytes) -> bytes:
    """
    Make SVG square by expanding viewBox/width/height on shorter dimension, centering content.
    If parsing fails, return original SVG unchanged.
    """
    try:
        root = ET.fromstring(svg_bytes)
        ns = {"svg": "http://www.w3.org/2000/svg"}
        ET.register_namespace("", "http://www.w3.org/2000/svg")

        # Get viewBox or width/height
        viewbox = root.get("viewBox")
        width_attr = root.get("width")
        height_attr = root.get("height")

        if viewbox:
            try:
                parts = viewbox.split()
                vx, vy, vw, vh = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
                # Make square
                if vw != vh:
                    max_dim = max(vw, vh)
                    offset_w = (max_dim - vw) / 2
                    offset_h = (max_dim - vh) / 2
                    root.set("viewBox", f"{vx - offset_w} {vy - offset_h} {max_dim} {max_dim}")
            except Exception:
                pass  # Malformed viewBox, leave it
        else:
            # Try width/height attributes
            try:
                w = float(width_attr) if width_attr and "%" not in width_attr else None
                h = float(height_attr) if height_attr and "%" not in height_attr else None
                if w and h and w != h:
                    max_dim = max(w, h)
                    root.set("width", str(max_dim))
                    root.set("height", str(max_dim))
            except Exception:
                pass

        return ET.tostring(root, encoding="utf-8")
    except Exception as e:
        log.debug(f"[svg-square] Failed to parse SVG: {e}")
        return svg_bytes  # Return original if parsing fails


def recolour_svg(svg_bytes: bytes, hex_colour: str) -> bytes:
    """
    Replace fill and stroke attributes (excluding 'none' and 'url(...)') with target colour.
    Also replaces inline style fill: and stroke: values.
    Returns modified SVG bytes.
    """
    try:
        svg_str = svg_bytes.decode("utf-8", errors="replace")

        # Replace fill="..." and stroke="..." attributes
        # Skip 'none' and 'url(...)' values
        svg_str = re.sub(
            r'fill="(?!none|url\()([^"]*)"',
            f'fill="{hex_colour}"',
            svg_str,
            flags=re.IGNORECASE
        )
        svg_str = re.sub(
            r'stroke="(?!none|url\()([^"]*)"',
            f'stroke="{hex_colour}"',
            svg_str,
            flags=re.IGNORECASE
        )

        # Replace inline style fill: and stroke: (without url(...) or none)
        svg_str = re.sub(
            r'fill\s*:\s*(?!none|url\()([^;}"]*)',
            f'fill: {hex_colour}',
            svg_str,
            flags=re.IGNORECASE
        )
        svg_str = re.sub(
            r'stroke\s*:\s*(?!none|url\()([^;}"]*)',
            f'stroke: {hex_colour}',
            svg_str,
            flags=re.IGNORECASE
        )

        return svg_str.encode("utf-8")
    except Exception as e:
        log.debug(f"[svg-recolour] Failed to recolour SVG: {e}")
        return svg_bytes  # Return original if processing fails


def _fetch_image(url: str, referer: str = "") -> tuple[Image.Image | None, bool, bytes | None]:
    """
    Download an image URL. Returns (PIL Image, is_svg, svg_raw_bytes).
    svg_raw_bytes is set even if rasterization failed, so we can still save the SVG.
    referer: optional Referer header (e.g. brand website) to help with CDN access.
    """
    # Fix protocol-relative URLs (//cdn.shopify.com/... → https://...)
    if url.startswith("//"):
        url = "https:" + url

    # Build image-specific headers with optional Referer
    hdrs = {**IMAGE_HEADERS}
    if referer:
        hdrs["Referer"] = referer
        hdrs["Origin"] = urlparse(referer).scheme + "://" + urlparse(referer).netloc

    # SVG handling: return raw bytes, no rasterization
    svg_data = None
    if _is_svg_url(url):
        svg_data = _fetch_svg(url)
        if svg_data:
            return None, True, svg_data  # Return SVG data without PIL Image

    try:
        resp = requests.get(url, headers=hdrs, timeout=TIMEOUT, stream=True)
        if resp.status_code != 200:
            log.debug(f"[fetch] HTTP {resp.status_code} for {url[:80]}")
            # Retry once with plain HEADERS if image headers failed
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            if resp.status_code != 200:
                log.debug(f"[fetch] Retry also failed: HTTP {resp.status_code}")
                return None, False, svg_data
        ct = resp.headers.get("Content-Type", "")
        # Check if response is SVG
        if "svg" in ct:
            svg_data = resp.content
            return None, True, svg_data  # Return SVG data without rasterizing
        if "image" not in ct and "octet" not in ct:
            log.debug(f"[fetch] Unexpected content-type '{ct}' for {url[:80]}")
            return None, False, svg_data
        img = Image.open(BytesIO(resp.content))
        if img.width < MIN_LOGO_SIZE or img.height < MIN_LOGO_SIZE:
            log.debug(f"[fetch] Image too small ({img.width}x{img.height}) from {url[:80]}")
            return None, False, svg_data
        return img, False, svg_data
    except Exception as e:
        log.debug(f"[fetch] Exception fetching {url[:80]}: {e}")
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
                    return {
                        "source": "brandfetch:logodev-svg",
                        "image": None, "svg_data": resp.content,
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
            except Exception as e:
                log.debug(f"[tier1-logodev] Error: {e}")
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
            except Exception as e:
                log.debug(f"[tier1-logodev-via-search] Error: {e}")
                pass

        # Fall back to the icon from search result
        if icon_url:
            img, is_svg, svg_raw = _fetch_image(icon_url)
            if img or (is_svg and svg_raw):  # Accept SVG even if image is None
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
    except Exception as e:
        log.debug(f"[tier1] Error: {e}")
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
    except Exception as e:
        log.debug(f"[tier2] Error fetching {website_url}: {e}")
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

    # Priority 0.5: Inline <svg> elements in header/nav/logo containers
    _inline_svg_bytes = None
    _logo_containers = soup.find_all(
        lambda tag: tag.name in ("header", "nav", "a", "div", "span")
        and any("logo" in (v if isinstance(v, str) else " ".join(v)).lower()
                for attr in ["class", "id", "aria-label"]
                for v in [tag.get(attr, "")] if v)
    )
    # Also check direct <svg> children of <a> tags with logo-ish hrefs
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        if href in ("/", website_url) or "home" in href.lower():
            _logo_containers.append(a_tag)
    for container in _logo_containers:
        svg_el = container.find("svg")
        if svg_el and not _inline_svg_bytes:
            svg_str = str(svg_el)
            if len(svg_str) > 100:  # skip trivial/icon SVGs
                # Ensure the SVG has xmlns for standalone rendering
                if "xmlns" not in svg_str:
                    svg_str = svg_str.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
                svg_bytes = svg_str.encode("utf-8")
                # Add as a candidate, not an early return — let it compete
                candidates.append(("inline-svg", website_url, 0.9))
                # Stash the SVG data for later retrieval (no rasterization)
                _inline_svg_bytes = svg_bytes

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
    fetched = []
    for source_type, url, confidence in candidates:
        # Inline SVGs already found — no fetch needed
        if source_type == "inline-svg" and _inline_svg_bytes:
            fetched.append({
                "source": "website:inline-svg",
                "image": None,
                "is_svg": True,
                "svg_data": _inline_svg_bytes,
                "url": url,
                "confidence": confidence,
                "theme_color": theme_color,
            })
            continue
        img, is_svg, svg_raw = _fetch_image(url, referer=website_url)
        if img or (is_svg and svg_raw):  # Accept SVG even if image is None
            r = {
                "source": f"website:{source_type}",
                "image": img,
                "is_svg": is_svg,
                "url": url,
                "confidence": confidence,
                "theme_color": theme_color,
            }
            if svg_raw:
                r["svg_data"] = svg_raw
            fetched.append(r)

    if not fetched:
        return None
    # Always return full list if multiple found
    if len(fetched) > 1:
        return fetched  # list of dicts
    return fetched[0]   # single dict


def _is_spa_html(html: str) -> bool:
    """Detect if HTML looks like a client-side SPA (Next.js, Nuxt, React, etc.)."""
    soup = BeautifulSoup(html, "html.parser")
    # SPA indicators: framework root divs with very few <img> tags
    spa_ids = {"__next", "__nuxt", "app", "root", "__app"}
    has_spa_root = any(soup.find(id=sid) for sid in spa_ids)
    img_count = len(soup.find_all("img"))
    # SPA pages often have <script> tags but few rendered images
    script_count = len(soup.find_all("script"))
    return has_spa_root and img_count <= 2 and script_count > 3


def tier2b_playwright_scrape(website_url: str) -> dict | None:
    """
    Fallback scraper using Playwright for SPA-rendered sites.
    Only called if tier2 returned None AND the raw HTML looks like a SPA.
    Requires: pip install playwright && python -m playwright install chromium
    """
    if not HAS_PLAYWRIGHT:
        return None
    if not website_url:
        return None

    try:
        # Quick check: is the raw HTML actually a SPA?
        resp = requests.get(website_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        if not _is_spa_html(resp.text):
            return None  # not a SPA, no need for Playwright

        log.info(f"[playwright] SPA detected, rendering {website_url}...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            page.goto(website_url, wait_until="networkidle", timeout=20000)
            html = page.content()
            browser.close()

        # Now parse the fully-rendered DOM — reuse tier2 logic
        soup = BeautifulSoup(html, "html.parser")
        base = website_url
        candidates = []

        # Inline SVGs in logo containers
        _logo_containers = soup.find_all(
            lambda tag: tag.name in ("header", "nav", "a", "div", "span")
            and any("logo" in (v if isinstance(v, str) else " ".join(v)).lower()
                    for attr in ["class", "id", "aria-label"]
                    for v in [tag.get(attr, "")] if v)
        )
        for container in _logo_containers:
            svg_el = container.find("svg")
            if svg_el and len(str(svg_el)) > 100:
                svg_str = str(svg_el)
                if "xmlns" not in svg_str:
                    svg_str = svg_str.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
                svg_bytes = svg_str.encode("utf-8")
                tc = soup.find("meta", attrs={"name": "theme-color"})
                return {
                    "source": "website:playwright-inline-svg",
                    "image": None, "is_svg": True, "svg_data": svg_bytes,
                    "url": website_url, "confidence": 0.88,
                    "theme_color": tc["content"].strip() if tc and tc.get("content") else None,
                }

        # SVG <img> tags
        for img_tag in soup.find_all("img", src=True):
            src = img_tag["src"]
            if _is_svg_url(src):
                alt = img_tag.get("alt", "")
                cls = " ".join(img_tag.get("class", []))
                if any("logo" in x.lower() for x in [src, alt, cls]):
                    candidates.append(("svg-img", urljoin(base, src), 0.9))

        # <img> with logo in class/alt/src
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src", "")
            alt = img_tag.get("alt", "")
            cls = " ".join(img_tag.get("class", []))
            if any("logo" in x.lower() for x in [src, alt, cls]):
                full_url = urljoin(base, src)
                if not _is_svg_url(full_url):
                    candidates.append(("img-logo", full_url, 0.75))

        # apple-touch-icon
        for link in soup.find_all("link", rel=lambda r: r and "apple-touch-icon" in " ".join(r).lower()):
            href = link.get("href")
            if href:
                candidates.append(("apple-touch-icon", urljoin(base, href), 0.8))

        # og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            candidates.append(("og:image", urljoin(base, og["content"]), 0.55))

        candidates.sort(key=lambda x: -x[2])
        theme_color = None
        tc = soup.find("meta", attrs={"name": "theme-color"})
        if tc and tc.get("content"):
            theme_color = tc["content"].strip()

        for source_type, url, confidence in candidates:
            img, is_svg, svg_raw = _fetch_image(url, referer=website_url)
            if img or (is_svg and svg_raw):  # Accept SVG even if image is None
                result = {
                    "source": f"website:playwright-{source_type}",
                    "image": img, "is_svg": is_svg,
                    "url": url, "confidence": confidence,
                    "theme_color": theme_color,
                }
                if svg_raw:
                    result["svg_data"] = svg_raw
                return result

    except Exception as e:
        log.debug(f"[playwright] Error: {e}")
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
                        return {
                            "source": "wikimedia:commons-svg",
                            "image": None, "svg_data": svg_data,
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
    except Exception as e:
        log.debug(f"[tier3-commons] Error: {e}")
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
                            return {
                                "source": "wikimedia:wikipedia-svg",
                                "image": None, "svg_data": found_svg_data,
                                "is_svg": True, "url": svg_url,
                                "confidence": 0.75,
                            }
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
    except Exception as e:
        log.debug(f"[tier3-wikipedia] Error: {e}")
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
    except Exception as e:
        log.debug(f"[wikimedia-file-url] Error: {e}")
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
    except Exception as e:
        log.debug(f"[tier4] Error: {e}")
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
            if img or (is_svg and svg_raw):  # Accept SVG even if image is None
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
    except Exception as e:
        log.debug(f"[tier5] Error: {e}")
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
                return {
                    "source": "gilbarbara-svg",
                    "image": None, "svg_data": resp.content,
                    "is_svg": True, "url": url,
                    "confidence": 0.75,
                }
        except Exception as e:
            log.debug(f"[tier6-gilbarbara] Error for {s}: {e}")
            continue

    return None


# ─── TIER 7: SEEKLOGO ──────────────────────────────────────────────────────

def tier7_seeklogo(brand_name: str) -> dict | None:
    """
    seeklogo.com — large collection of vector logos.
    Scrapes the search page for SVG download links.
    """
    slug = brand_name.lower().replace(" ", "+")
    search_url = f"https://seeklogo.com/search?q={slug}"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        # Find first logo result link
        result_link = soup.select_one("a.logo-item, a[href*='/vector-logos/'], .search-results a[href*='logo']")
        if not result_link:
            # Try broader: any link to a logo detail page
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/vector-logos/" in href or "-logo-" in href:
                    result_link = a
                    break
        if not result_link:
            return None

        detail_url = result_link["href"]
        if not detail_url.startswith("http"):
            detail_url = urljoin("https://seeklogo.com", detail_url)

        # Fetch detail page to find the SVG/PNG download
        resp2 = requests.get(detail_url, headers=HEADERS, timeout=TIMEOUT)
        if resp2.status_code != 200:
            return None
        soup2 = BeautifulSoup(resp2.text, "html.parser")

        # Look for download link (SVG preferred, then PNG)
        img_url = None
        is_svg = False
        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if ".svg" in href.lower():
                img_url = href if href.startswith("http") else urljoin(detail_url, href)
                is_svg = True
                break
        if not img_url:
            # Try PNG
            for a in soup2.find_all("a", href=True):
                href = a["href"]
                if ".png" in href.lower() and ("download" in a.text.lower() or "logo" in href.lower()):
                    img_url = href if href.startswith("http") else urljoin(detail_url, href)
                    break
        # Also try og:image as fallback
        if not img_url:
            og = soup2.find("meta", property="og:image")
            if og and og.get("content"):
                img_url = og["content"]

        if not img_url:
            return None

        img, found_svg, svg_raw = _fetch_image(img_url)
        if not img and not (found_svg and svg_raw):
            return None

        result = {
            "source": "seeklogo",
            "image": img, "is_svg": is_svg or found_svg,
            "url": img_url, "confidence": 0.7,
        }
        if svg_raw:
            result["svg_data"] = svg_raw
        return result
    except Exception as e:
        log.debug(f"[tier7] Error: {e}")
        return None


# ─── TIER 8: SIMPLE ICONS ──────────────────────────────────────────────────

def tier8_simple_icons(brand_name: str) -> dict | None:
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
        except Exception as e:
            log.debug(f"[tier8-si-colour] Error: {e}")
            pass

        return {
            "source": "simpleicons-svg",
            "image": None, "svg_data": resp.content,
            "is_svg": True, "url": url,
            "confidence": 0.65,
            "si_colour": si_colour,  # bonus: brand colour from their DB
        }
    except Exception as e:
        log.debug(f"[tier8] Error: {e}")
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
    """Make logo square with padding. Preserve resolution — upscale if below TARGET_SIZE, keep native if above."""
    img = img.convert("RGBA")

    # Make square
    w, h = img.size
    if w != h:
        size = max(w, h)
        padded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        padded.paste(img, ((size - w) // 2, (size - h) // 2), img)
        img = padded

    # Resize: upscale if below TARGET_SIZE, keep native resolution if above
    if img.width < TARGET_SIZE:
        img = img.resize((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)
    # If above TARGET_SIZE, keep native resolution

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

    # Check 1: Aspect ratio — logos can be wide wordmarks (up to ~10:1) or square
    # Only flag truly extreme ratios (banners, vertical badges)
    aspect = w / max(h, 1)
    if aspect < 0.2 or aspect > 10.0:
        issues.append(f"unusual aspect ratio ({aspect:.1f})")
        score *= 0.5
    elif aspect > 7.0:
        issues.append(f"very wide wordmark ({aspect:.1f})")
        # mild note, no penalty — wide text logos are common

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
    """Generate interactive HTML review page with colour override, mark-as-final, categories, and candidate selection."""
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

        # Find the selected/primary logo and use its thumb_b64 as logo_b64
        logo_b64 = ""
        candidates = r.get("logo_candidates", [])
        if candidates:
            # Find the selected candidate, or use the first one
            sel_cand = next((c for c in candidates if c.get("is_selected")), candidates[0])
            logo_b64 = sel_cand.get("thumb_b64", "")

        # Legacy fallback: read from disk if no candidates
        if not logo_b64:
            logo_path = out_dir / safe_name / "logo.png"
            if logo_path.exists():
                logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()

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
            "undersize": r.get("undersize", False),
            "original_size": r.get("original_size", ""),
            "category": r.get("category", "Uncategorized"),
            "meta_description": r.get("meta_description", ""),
            "logo_quality_score": r.get("logo_quality_score", 1.0),
            "logo_issues": r.get("logo_issues", []),
            "logo_b64": logo_b64,
            "logo_candidates": candidates,  # Full candidate list with thumb_b64, is_svg, file, tier, tier_name, source, confidence, size, svg_markup
        })

    brands_json = json.dumps(brands_js, cls=SafeEncoder)
    failed_json = json.dumps([{"name": r["brand_name"], "errors": r.get("errors", [])} for r in failed])
    categories_json = json.dumps(all_categories)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brand Asset Pipeline v6 — Review</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f5f5; color:#222; }}
  .toolbar {{ position:sticky; top:0; z-index:100; background:#1a1a2e; color:#fff; padding:12px 24px; display:flex; align-items:center; gap:20px; flex-wrap:wrap; box-shadow:0 2px 12px rgba(0,0,0,0.15); }}
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
  .card {{ background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,0.08); transition:box-shadow 0.2s; cursor:pointer; }}
  .card:hover {{ box-shadow:0 4px 16px rgba(0,0,0,0.12); }}
  .card.flagged {{ outline:3px solid #ff4444; }}
  .card.finalized {{ outline:3px solid #4caf50; }}
  .card-preview {{ aspect-ratio:1; display:flex; align-items:center; justify-content:center; overflow:hidden; position:relative; }}
  .card-preview.shape-circle {{  }}
  .card-preview.shape-card {{ aspect-ratio:14/9; }}
  .card-preview img {{ width:60%; height:60%; object-fit:contain; transition:all 0.2s; }}
  .shape-circle img {{ border-radius:50%; }}
  .card-preview .svg-overlay {{ width:60%; height:60%; display:flex; align-items:center; justify-content:center; }}
  .card-preview .svg-overlay svg {{ width:100%; height:100%; }}
  .card-preview .expand-hint {{ position:absolute; bottom:4px; right:6px; font-size:9px; color:rgba(255,255,255,0.7); background:rgba(0,0,0,0.3); padding:1px 5px; border-radius:4px; pointer-events:none; }}
  .card-meta {{ padding:10px 12px; font-size:11px; line-height:1.6; border-top:1px solid #f0f0f0; }}
  .card-meta .brand-name {{ font-weight:600; font-size:13px; margin-bottom:2px; }}
  .card-meta .meta-row {{ display:flex; justify-content:space-between; color:#666; }}
  .card-meta .description {{ font-size:10px; color:#888; margin-top:4px; max-height:32px; overflow:hidden; }}
  .website-link {{ font-size:10px; color:#1a73e8; text-decoration:none; display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .website-link:hover {{ text-decoration:underline; }}
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
  .btn-final {{ flex:1; padding:4px 8px; border:1px solid #4caf50; border-radius:6px; background:#fff; color:#4caf50; font-size:10px; font-weight:600; cursor:pointer; }}
  .btn-final:hover {{ background:#e8f5e9; }}
  .btn-final.done {{ background:#4caf50; color:#fff; }}
  .btn-flag {{ padding:4px 8px; border:1px solid #ff5722; border-radius:6px; background:#fff; color:#ff5722; font-size:10px; cursor:pointer; }}
  .btn-flag:hover {{ background:#fbe9e7; }}
  .btn-flag.active {{ background:#ff5722; color:#fff; }}
  .flag-menu {{ position:absolute; background:#fff; border:1px solid #ddd; border-radius:8px; box-shadow:0 4px 16px rgba(0,0,0,0.15); padding:6px 0; z-index:200; min-width:160px; }}
  .flag-menu-item {{ padding:8px 14px; font-size:12px; cursor:pointer; display:flex; align-items:center; gap:6px; }}
  .flag-menu-item:hover {{ background:#f5f5f5; }}
  .flag-menu-item .flag-dot {{ width:8px; height:8px; border-radius:50%; }}
  .flag-badge {{ display:inline-block; padding:1px 6px; border-radius:10px; font-size:9px; font-weight:600; }}
  .flag-badge.wrong-logo {{ background:#ffcdd2; color:#c62828; }}
  .flag-badge.needs-upscaling {{ background:#fff3e0; color:#e65100; }}
  .flag-badge.wrong-colour {{ background:#e8eaf6; color:#283593; }}
  .flag-badge.other {{ background:#f3e5f5; color:#6a1b9a; }}
  .filter-bar {{ padding:6px 24px; background:#fff; border-bottom:1px solid #eee; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  .filter-btn {{ padding:3px 10px; border:1px solid #ddd; border-radius:16px; background:#fff; font-size:11px; cursor:pointer; }}
  .filter-btn:hover {{ background:#f0f0f0; }}
  .filter-btn.active {{ background:#1a1a2e; color:#fff; border-color:#1a1a2e; }}
  .export-bar {{ padding:12px 24px; background:#fff; border-top:1px solid #eee; position:sticky; bottom:0; display:flex; align-items:center; gap:12px; z-index:50; flex-wrap:wrap; }}
  .btn-export {{ padding:8px 20px; background:#1a1a2e; color:#fff; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }}
  .btn-export:hover {{ background:#2a2a4e; }}
  .failed-section {{ padding:0 24px 24px; }}
  .failed-section h2 {{ font-size:14px; color:#888; margin-bottom:8px; }}
  .failed-item {{ font-size:12px; color:#999; padding:2px 0; }}

  /* ─── EXPANDED DETAIL PANEL ─── */
  .detail-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:200; justify-content:center; align-items:center; }}
  .detail-overlay.open {{ display:flex; }}
  .detail-panel {{ background:#fff; border-radius:16px; width:90vw; max-width:900px; max-height:90vh; overflow-y:auto; box-shadow:0 8px 40px rgba(0,0,0,0.25); }}
  .detail-header {{ display:flex; align-items:center; gap:16px; padding:20px 24px; border-bottom:1px solid #eee; }}
  .detail-header h2 {{ font-size:20px; font-weight:600; flex:1; }}
  .detail-header .close-btn {{ width:32px; height:32px; border-radius:50%; border:none; background:#f0f0f0; font-size:18px; cursor:pointer; display:flex; align-items:center; justify-content:center; }}
  .detail-header .close-btn:hover {{ background:#ddd; }}
  .detail-body {{ padding:24px; display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  .detail-left {{ display:flex; flex-direction:column; gap:16px; }}
  .detail-preview {{ aspect-ratio:1; border-radius:12px; display:flex; align-items:center; justify-content:center; max-height:300px; }}
  .detail-preview img {{ width:60%; height:60%; object-fit:contain; }}
  .detail-preview .svg-overlay {{ width:60%; height:60%; display:flex; align-items:center; justify-content:center; }}
  .detail-preview .svg-overlay svg {{ width:100%; height:100%; }}
  .detail-right {{ display:flex; flex-direction:column; gap:12px; }}
  .detail-info {{ font-size:13px; line-height:1.8; }}
  .detail-info dt {{ font-weight:600; color:#666; font-size:11px; text-transform:uppercase; }}
  .detail-info dd {{ margin-bottom:8px; }}
  .logo-picker {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(120px,1fr)); gap:10px; }}
  .logo-option {{ border:2px solid #eee; border-radius:10px; padding:8px; text-align:center; cursor:pointer; transition:all 0.15s; background:#fff; }}
  .logo-option:hover {{ border-color:#1a73e8; box-shadow:0 2px 8px rgba(0,0,0,0.08); }}
  .logo-option.selected {{ border-color:#4caf50; background:#f0faf0; }}
  .logo-option img {{ width:80px; height:80px; object-fit:contain; display:block; margin:0 auto 6px; }}
  .logo-option .lo-label {{ font-size:10px; color:#666; }}
  .logo-option .lo-source {{ font-size:9px; color:#999; }}
  .lo-path {{ font-size:8px; color:#aaa; word-break:break-all; margin-top:2px; font-family:monospace; }}
  .lo-format {{ font-size:8px; display:inline-block; margin-top:2px; padding:1px 4px; border-radius:3px; background:#eee; color:#666; }}
  .recolour-section {{ padding:12px 0; border-top:1px solid #eee; }}
  .recolour-section h4 {{ font-size:12px; color:#666; margin-bottom:8px; }}
  .recolour-controls {{ display:flex; align-items:center; gap:10px; }}
  .recolour-controls input[type=color] {{ width:36px; height:36px; border:2px solid #ddd; border-radius:8px; cursor:pointer; }}
  .recolour-controls button {{ padding:6px 12px; border:1px solid #ccc; border-radius:6px; background:#fff; font-size:11px; cursor:pointer; }}
  .colour-section h4 {{ font-size:12px; color:#666; margin-bottom:6px; }}
  @media (max-width:700px) {{ .detail-body {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="toolbar">
  <h1>Brand Assets v6</h1>
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
  <button class="filter-btn" data-filter="flag-wrong-logo" style="font-size:10px;">Wrong Logo</button>
  <button class="filter-btn" data-filter="flag-needs-upscaling" style="font-size:10px;">Needs Upscaling</button>
  <button class="filter-btn" data-filter="flag-wrong-colour" style="font-size:10px;">Wrong Colour</button>
  <button class="filter-btn" data-filter="low">Low risk</button>
  <button class="filter-btn" data-filter="high">High risk</button>
  <button class="filter-btn" data-filter="svg">SVG</button>
  <button class="filter-btn" data-filter="logoissue">Logo issues</button>
</div>

<div class="grid" id="grid"></div>
<div class="failed-section" id="failedSection"></div>

<div class="export-bar">
  <button class="btn-export" onclick="exportJSON()">Export JSON</button>
  <button class="btn-export" style="background:#555;" onclick="exportCSV()">Export CSV</button>
  <button class="btn-export" style="background:#2e7d32;" onclick="exportZIP()">Export ZIP (assets + data)</button>
  <span id="exportStatus" style="font-size:12px;color:#666;"></span>
</div>

<!-- Detail overlay -->
<div class="detail-overlay" id="detailOverlay" onclick="if(event.target===this)closeDetail()">
  <div class="detail-panel" id="detailPanel"></div>
</div>

<script>
const BRANDS = {brands_json};
const FAILED = {failed_json};
const CATEGORIES = {categories_json};

let finalized = new Set();
let flagged = {{}};  // folder -> reason string (folder -> reason)
let selectedColours = {{}};
let selectedLogos = {{}};   // folder -> candidate index
let logoRecolours = {{}};
let activeFilter = "all";
let activeCategory = "all";
BRANDS.forEach(b => {{ selectedColours[b.folder] = b.colour; }});

const catSel = document.getElementById("categorySelect");
CATEGORIES.forEach(c => {{ const o = document.createElement("option"); o.value = c; o.textContent = c; catSel.appendChild(o); }});

function getActiveColour(b) {{ return selectedColours[b.folder] || b.colour; }}
function getActiveLogo(b) {{
  const idx = selectedLogos[b.folder];
  if (idx !== undefined && b.logo_candidates && b.logo_candidates[idx]) {{
    return b.logo_candidates[idx].thumb_b64;
  }}
  return b.logo_b64;
}}

function switchColour(folder, hex, evt) {{
  if (evt) evt.stopPropagation();
  selectedColours[folder] = hex;
  const card = document.querySelector(`.card[data-folder="${{folder}}"]`);
  if (!card) return;
  card.querySelector(".card-preview").style.background = hex;
  card.querySelectorAll(".colour-swatch").forEach(sw => sw.classList.toggle("active", sw.dataset.hex === hex));
  const hexSpan = card.querySelector(".hex-display");
  if (hexSpan) hexSpan.textContent = hex;
}}

function customColour(folder, inputEl, evt) {{
  if (evt) evt.stopPropagation();
  switchColour(folder, inputEl.value);
}}

function selectLogo(folder, candIdx) {{
  selectedLogos[folder] = candIdx;
  const b = BRANDS.find(x => x.folder === folder);
  if (!b) return;
  // Update detail panel preview
  const cand = b.logo_candidates[candIdx];
  const preview = document.querySelector("#detailPanel .detail-preview");
  if (preview) {{
    const img = preview.querySelector("img");
    if (img && cand.thumb_b64) img.src = "data:image/png;base64," + cand.thumb_b64;
  }}
  // Mark selected in picker
  document.querySelectorAll("#detailPanel .logo-option").forEach((el, i) => {{
    el.classList.toggle("selected", i === candIdx);
  }});
  // Update grid card too
  renderGrid();
}}

function recolourSVG(folder, hex) {{
  logoRecolours[folder] = hex;
  // Update detail panel preview if open
  const b = BRANDS.find(x => x.folder === folder);
  if (!b) return;
  const svgSrc = _getActiveSvgMarkup(b);
  if (!svgSrc) return;
  const recoloured = _applyRecolour(svgSrc, hex);
  const overlay = document.querySelector("#detailPanel .detail-preview .svg-overlay");
  if (overlay) {{
    overlay.innerHTML = recoloured;
    overlay.style.display = "flex";
    const img = document.querySelector("#detailPanel .detail-preview img");
    if (img) img.style.display = "none";
  }}
  renderGrid();
}}

function resetLogoColour(folder) {{
  delete logoRecolours[folder];
  renderGrid();
  // Refresh detail panel if open
  const b = BRANDS.find(x => x.folder === folder);
  if (b) openDetail(b.folder);
}}

function _getActiveSvgMarkup(b) {{
  const idx = selectedLogos[b.folder];
  if (idx !== undefined && b.logo_candidates[idx] && b.logo_candidates[idx].svg_markup) {{
    return b.logo_candidates[idx].svg_markup;
  }}
  return b.svg_markup || "";
}}

function _applyRecolour(svg, hex) {{
  svg = svg.replace(/fill="(?!none|url)([^"]*)"/gi, `fill="${{hex}}"`);
  svg = svg.replace(/stroke="(?!none|url)([^"]*)"/gi, `stroke="${{hex}}"`);
  svg = svg.replace(/fill:\\s*(?!none|url)[^;"]+/gi, `fill:${{hex}}`);
  svg = svg.replace(/stroke:\\s*(?!none|url)[^;"]+/gi, `stroke:${{hex}}`);
  return svg;
}}

function toggleFinal(folder, evt) {{
  if (evt) evt.stopPropagation();
  if (finalized.has(folder)) finalized.delete(folder); else finalized.add(folder);
  renderGrid();
}}

const FLAG_REASONS = [
  {{id:"wrong-logo", label:"Wrong Logo", dot:"#c62828"}},
  {{id:"needs-upscaling", label:"Needs Upscaling", dot:"#e65100"}},
  {{id:"wrong-colour", label:"Wrong Colour", dot:"#283593"}},
  {{id:"other", label:"Other", dot:"#6a1b9a"}},
];
let _flagMenuOpen = null;

function showFlagMenu(folder, evt) {{
  if (evt) evt.stopPropagation();
  // Close existing menu
  closeFlagMenu();
  if (flagged[folder]) {{
    // Already flagged — unflag
    delete flagged[folder];
    renderGrid();
    return;
  }}
  const btn = evt.currentTarget;
  const rect = btn.getBoundingClientRect();
  const menu = document.createElement("div");
  menu.className = "flag-menu";
  menu.style.left = rect.left + "px";
  menu.style.top = (rect.bottom + 4) + "px";
  menu.style.position = "fixed";
  FLAG_REASONS.forEach(r => {{
    const item = document.createElement("div");
    item.className = "flag-menu-item";
    item.innerHTML = `<span class="flag-dot" style="background:${{r.dot}}"></span>${{r.label}}`;
    item.onclick = (e) => {{
      e.stopPropagation();
      flagged[folder] = r.id;
      closeFlagMenu();
      renderGrid();
    }};
    menu.appendChild(item);
  }});
  document.body.appendChild(menu);
  _flagMenuOpen = menu;
}}

function closeFlagMenu() {{
  if (_flagMenuOpen) {{ _flagMenuOpen.remove(); _flagMenuOpen = null; }}
}}

document.addEventListener("click", () => closeFlagMenu());

// ─── DETAIL PANEL ───
function openDetail(folder) {{
  const b = BRANDS.find(x => x.folder === folder);
  if (!b) return;
  const ac = getActiveColour(b);
  const hasSvg = _getActiveSvgMarkup(b).length > 0;
  const recolourHex = logoRecolours[b.folder] || "";

  // Preview: show recoloured SVG if applicable, else PNG
  let previewContent = "";
  if (hasSvg && recolourHex) {{
    const rc = _applyRecolour(_getActiveSvgMarkup(b), recolourHex);
    previewContent = `<img src="data:image/png;base64,${{getActiveLogo(b)}}" style="display:none"><div class="svg-overlay" style="display:flex">${{rc}}</div>`;
  }} else {{
    previewContent = `<img src="data:image/png;base64,${{getActiveLogo(b)}}"><div class="svg-overlay" style="display:none"></div>`;
  }}

  // Logo candidates picker
  let logoPicker = "";
  if (b.logo_candidates && b.logo_candidates.length > 0) {{
    const selIdx = selectedLogos[b.folder] !== undefined ? selectedLogos[b.folder] : b.logo_candidates.findIndex(c => c.is_selected);
    logoPicker = `<h4 style="font-size:12px;color:#666;margin-bottom:8px;">Logo options (${{b.logo_candidates.length}})</h4><div class="logo-picker">`;
    b.logo_candidates.forEach((c, i) => {{
      const sel = i === selIdx ? "selected" : "";
      const svgBadge = c.is_svg ? ' <span class="badge badge-svg" style="font-size:8px;">SVG</span>' : ' <span class="badge" style="background:#ddd;color:#555;font-size:8px;">PNG</span>';
      const dims = c.size || "unknown";
      const filePath = c.file || "unknown";
      logoPicker += `<div class="logo-option ${{sel}}" onclick="selectLogo('${{b.folder}}',${{i}})">
        <img src="data:image/png;base64,${{c.thumb_b64}}" alt="Option ${{i+1}}">
        <div class="lo-label">T${{c.tier}}: ${{c.tier_name}}</div>
        <div class="lo-source">${{dims}} | ${{Math.round(c.confidence*100)}}%</div>
        <div class="lo-path">${{filePath}}</div>
        <div class="lo-format">${{svgBadge}}</div>
      </div>`;
    }});
    logoPicker += "</div>";
  }} else {{
    logoPicker = `<p style="font-size:11px;color:#999;">Single logo found (run with --multi for options)</p>`;
  }}

  // Recolour section
  let recolourSection = "";
  if (hasSvg) {{
    recolourSection = `<div class="recolour-section">
      <h4>Recolour SVG logo</h4>
      <div class="recolour-controls">
        <input type="color" value="${{recolourHex || '#FFFFFF'}}" onchange="recolourSVG('${{b.folder}}',this.value)">
        <button onclick="recolourSVG('${{b.folder}}','#FFFFFF')">White</button>
        <button onclick="recolourSVG('${{b.folder}}','#000000')">Black</button>
        <button onclick="resetLogoColour('${{b.folder}}')">Reset</button>
      </div>
    </div>`;
  }}

  // Colour swatches
  let colourHtml = `<div class="colour-section"><h4>Background colour</h4><div class="colour-options">`;
  const cands = b.colour_candidates || [{{hex:b.colour, source:"auto"}}];
  for (const c of cands) {{
    const isAct = c.hex === ac;
    const bdr = c.hex === "#FFFFFF" ? "border:1px solid #ddd;" : "";
    colourHtml += `<div class="colour-swatch ${{isAct?"active":""}}" style="background:${{c.hex}};${{bdr}}; width:28px;height:28px;" data-hex="${{c.hex}}" title="${{c.hex}} (${{c.source}})" onclick="switchColour('${{b.folder}}','${{c.hex}}');openDetail('${{b.folder}}')"></div>`;
  }}
  colourHtml += `<input type="color" class="colour-input" value="${{ac}}" style="width:28px;height:28px;" onchange="switchColour('${{b.folder}}',this.value);openDetail('${{b.folder}}')" title="Custom colour">`;
  colourHtml += `</div></div>`;

  const websiteLink = b.website ? `<dd><a href="${{b.website}}" target="_blank" style="color:#1a73e8;">${{b.website}}</a></dd>` : "<dd>N/A</dd>";
  const isFinal = finalized.has(b.folder);
  const isFlagged = !!flagged[b.folder];

  document.getElementById("detailPanel").innerHTML = `
    <div class="detail-header">
      <h2>${{b.name}}</h2>
      <span class="category-tag" style="font-size:11px;">${{b.category}}</span>
      <button class="btn-final ${{isFinal?"done":""}}" style="padding:6px 16px;font-size:12px;" onclick="toggleFinal('${{b.folder}}');openDetail('${{b.folder}}')">${{isFinal?"Finalized":"Mark Final"}}</button>
      <button class="btn-flag ${{isFlagged?"active":""}}" style="padding:6px 12px;font-size:12px;" onclick="showFlagMenu('${{b.folder}}',event)">${{isFlagged?"Unflag":"Flag"}}</button>
      ${{isFlagged ? `<span class="flag-badge ${{flagged[b.folder]}}" style="font-size:11px;padding:3px 8px;">${{FLAG_REASONS.find(r=>r.id===flagged[b.folder])?.label || flagged[b.folder]}}</span>` : ""}}
      <button class="close-btn" onclick="closeDetail()">&times;</button>
    </div>
    <div class="detail-body">
      <div class="detail-left">
        <div class="detail-preview" style="background:${{ac}}">
          ${{previewContent}}
        </div>
        ${{colourHtml}}
        ${{recolourSection}}
      </div>
      <div class="detail-right">
        <div class="detail-info">
          <dt>Website</dt>
          ${{websiteLink}}
          <dt>Source</dt>
          <dd>Tier ${{b.tier}} (${{b.source}}) &mdash; ${{Math.round(b.confidence*100)}}% confidence</dd>
          <dt>Size</dt>
          <dd>${{b.original_size}}${{b.undersize?" (upscaled)":""}}</dd>
          <dt>Background colour</dt>
          <dd><span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:${{ac}};vertical-align:middle;border:1px solid #ddd;"></span> ${{ac}}</dd>
          ${{b.meta_description ? `<dt>Description</dt><dd style="font-size:11px;color:#666;">${{b.meta_description}}</dd>` : ""}}
          ${{b.logo_issues && b.logo_issues.length ? `<dt>Logo issues</dt><dd style="color:#c62828;font-size:11px;">${{b.logo_issues.join(", ")}}</dd>` : ""}}
        </div>
        ${{logoPicker}}
      </div>
    </div>`;

  document.getElementById("detailOverlay").classList.add("open");
}}

function closeDetail() {{
  document.getElementById("detailOverlay").classList.remove("open");
}}

document.addEventListener("keydown", e => {{ if (e.key === "Escape") closeDetail(); }});

// ─── FILTERS & SORTING ───
function applyFilters(brands) {{
  if (activeFilter === "finalized") brands = brands.filter(b => finalized.has(b.folder));
  else if (activeFilter === "pending") brands = brands.filter(b => !finalized.has(b.folder));
  else if (activeFilter === "flagged") brands = brands.filter(b => !!flagged[b.folder]);
  else if (activeFilter === "flag-wrong-logo") brands = brands.filter(b => flagged[b.folder] === "wrong-logo");
  else if (activeFilter === "flag-needs-upscaling") brands = brands.filter(b => flagged[b.folder] === "needs-upscaling");
  else if (activeFilter === "flag-wrong-colour") brands = brands.filter(b => flagged[b.folder] === "wrong-colour");
  else if (activeFilter === "low") brands = brands.filter(b => b.blending_risk === "LOW");
  else if (activeFilter === "high") brands = brands.filter(b => b.blending_risk === "HIGH");
  else if (activeFilter === "svg") brands = brands.filter(b => (b.logo_candidates && b.logo_candidates.some(c => c.is_svg)) || b.is_svg);
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
    const isFlagged = !!flagged[b.folder];
    const shapeClass = shape === "circle" ? "shape-circle" : shape === "card" ? "shape-card" : "";
    const ac = getActiveColour(b);
    const rc = b.blending_risk === "LOW" ? "badge-low" : b.blending_risk === "MEDIUM" ? "badge-medium" : "badge-high";
    const logoSrc = getActiveLogo(b);
    const hasSvg = _getActiveSvgMarkup(b).length > 0;
    const recolourHex = logoRecolours[b.folder];

    let badges = `<span class="badge ${{rc}}">${{b.blending_risk}}</span> `;
    if ((b.logo_candidates && b.logo_candidates.some(c => c.is_svg)) || b.is_svg) badges += `<span class="badge badge-svg">SVG</span> `;
    if (b.bg_removed) badges += `<span class="badge badge-rembg">BG Rem</span> `;
    if (b.undersize) badges += `<span class="badge badge-undersize">Upscaled</span> `;
    if (b.logo_issues && b.logo_issues.length) badges += `<span class="badge badge-logoissue" title="${{b.logo_issues.join(', ')}}">!</span> `;
    const candCount = (b.logo_candidates && b.logo_candidates.length > 1) ? `<span class="badge" style="background:#f0f0f0;color:#333;">${{b.logo_candidates.length}} opts</span> ` : "";

    let swatches = `<span class="label">BG:</span>`;
    const cands = b.colour_candidates || [{{hex:b.colour, source:"auto", blending_risk:b.blending_risk}}];
    for (const c of cands) {{
      const isAct = c.hex === ac;
      const rd = c.blending_risk === "LOW" ? "r-low" : c.blending_risk === "MEDIUM" ? "r-medium" : c.blending_risk === "HIGH" ? "r-high" : "";
      const bdr = c.hex === "#FFFFFF" ? "border:1px solid #ddd;" : "";
      swatches += `<div class="colour-swatch ${{isAct?"active":""}}" style="background:${{c.hex}};${{bdr}}" data-hex="${{c.hex}}" title="${{c.hex}} (${{c.source}})" onclick="switchColour('${{b.folder}}','${{c.hex}}',event)">${{rd?`<span class="risk-dot ${{rd}}"></span>`:""}}</div>`;
    }}
    swatches += `<input type="color" class="colour-input" value="${{ac}}" onchange="customColour('${{b.folder}}',this,event)" onclick="event.stopPropagation()" title="Custom colour">`;

    // Preview: show recoloured SVG or PNG
    let previewImg = "";
    if (hasSvg && recolourHex) {{
      const rcSvg = _applyRecolour(_getActiveSvgMarkup(b), recolourHex);
      previewImg = `<img src="data:image/png;base64,${{logoSrc}}" style="display:none"><div class="svg-overlay" style="display:flex">${{rcSvg}}</div>`;
    }} else {{
      previewImg = `<img src="data:image/png;base64,${{logoSrc}}" alt="${{b.name}}" loading="lazy"><div class="svg-overlay" style="display:none"></div>`;
    }}

    const websiteLink = b.website ? `<a class="website-link" href="${{b.website}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${{b.website.replace(/^https?:\\/\\//, "").replace(/\\/$/,"")}}</a>` : "";
    const flagReason = flagged[b.folder] || "";
    const flagBadge = flagReason ? `<span class="flag-badge ${{flagReason}}">${{FLAG_REASONS.find(r=>r.id===flagReason)?.label || flagReason}}</span>` : "";

    html += `
      <div class="card ${{isFinal?"finalized":""}} ${{isFlagged?"flagged":""}}" data-folder="${{b.folder}}" onclick="openDetail('${{b.folder}}')">
        <div class="card-preview ${{shapeClass}}" style="background:${{ac}}">
          ${{previewImg}}
          <span class="expand-hint">Click to expand</span>
        </div>
        <div class="card-meta">
          <div class="brand-name">${{b.name}}</div>
          ${{websiteLink}}
          <span class="category-tag">${{b.category}}</span>
          <div class="meta-row"><span class="hex-display">${{ac}}</span><span>T${{b.tier}} ${{Math.round(b.confidence*100)}}%</span></div>
          <div style="margin-top:3px">${{badges}}${{candCount}}${{flagBadge}}</div>
          <div class="colour-options">${{swatches}}</div>
          <div class="card-actions">
            <button class="btn-final ${{isFinal?"done":""}}" onclick="toggleFinal('${{b.folder}}',event)">${{isFinal?"Finalized":"Mark Final"}}</button>
            <button class="btn-flag ${{isFlagged?"active":""}}" onclick="showFlagMenu('${{b.folder}}',event)">${{isFlagged?"Unflag":"Flag"}}</button>
          </div>
        </div>
      </div>`;
  }}
  grid.innerHTML = html;

  const finalCount = finalized.size;
  document.getElementById("statTotal").textContent = `${{brands.length}} / ${{BRANDS.length}}`;
  document.getElementById("statFinal").textContent = `Finalized: ${{finalCount}}`;
  document.getElementById("statFlagged").textContent = `Flagged: ${{Object.keys(flagged).length}}`;
  document.getElementById("progressFill").style.width = `${{Math.round(finalCount/BRANDS.length*100)}}%`;
}}

// ─── EXPORTS ───
function _buildExportData() {{
  return BRANDS.filter(b => finalized.has(b.folder)).map(b => {{
    const selIdx = selectedLogos[b.folder];
    const selCand = selIdx !== undefined && b.logo_candidates ? b.logo_candidates[selIdx] : null;
    const selFile = selCand ? selCand.file : (b.is_svg ? b.folder + "/logo.svg" : b.folder + "/logo.png");
    const fmt = selCand ? (selCand.is_svg ? "svg" : "png") : (b.is_svg ? "svg" : "png");
    return {{
      brand_name: b.name,
      folder: b.folder,
      category: b.category,
      website: b.website || "",
      bg_colour: selectedColours[b.folder] || b.colour,
      logo_recolour: logoRecolours[b.folder] || null,
      meta_description: b.meta_description || "",
      selected_file: selFile,
      format: fmt,
      logo_source: b.source,
      source_tier: b.tier,
      confidence: b.confidence,
      blending_risk: b.blending_risk,
      logo_quality_score: b.logo_quality_score,
      logo_issues: b.logo_issues || [],
      original_size: b.original_size,
      undersize: b.undersize,
      bg_removed: b.bg_removed,
      flag_reason: flagged[b.folder] || null,
    }};
  }});
}}

function exportJSON() {{
  const data = _buildExportData();
  if (!data.length) {{ document.getElementById("exportStatus").textContent = "No finalized brands"; return; }}
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: "application/json"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "approved_brands.json"; a.click();
  document.getElementById("exportStatus").textContent = `Exported ${{data.length}} brands (JSON)`;
}}

function exportCSV() {{
  const data = _buildExportData();
  if (!data.length) {{ document.getElementById("exportStatus").textContent = "No finalized brands"; return; }}
  const flat = data.map(d => ({{ ...d, logo_issues: (d.logo_issues||[]).join("; "), logo_recolour: d.logo_recolour||"" }}));
  const headers = Object.keys(flat[0]);
  const csv = [headers.join(","), ...flat.map(r => headers.map(h => `"${{String(r[h]||"").replace(/"/g,'""')}}"`).join(","))].join("\\n");
  const blob = new Blob([csv], {{type:"text/csv"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "approved_brands.csv"; a.click();
  document.getElementById("exportStatus").textContent = `Exported ${{data.length}} brands (CSV)`;
}}

async function exportZIP() {{
  const data = _buildExportData();
  if (!data.length) {{ document.getElementById("exportStatus").textContent = "No finalized brands"; return; }}
  document.getElementById("exportStatus").textContent = "Building ZIP...";
  const zip = new JSZip();

  // Add data CSV and JSON
  const flat = data.map(d => ({{ ...d, logo_issues: (d.logo_issues||[]).join("; "), logo_recolour: d.logo_recolour||"" }}));
  const headers = Object.keys(flat[0]);
  const csvStr = [headers.join(","), ...flat.map(r => headers.map(h => `"${{String(r[h]||"").replace(/"/g,'""')}}"`).join(","))].join("\\n");
  zip.file("brand_data.csv", csvStr);
  zip.file("brand_data.json", JSON.stringify(data, null, 2));

  // Add logos sorted by flag status
  for (const d of data) {{
    const b = BRANDS.find(x => x.folder === d.folder);
    if (!b) continue;
    const selIdx = selectedLogos[b.folder];
    const selCand = selIdx !== undefined && b.logo_candidates ? b.logo_candidates[selIdx] : null;

    // Determine flag folder
    let flagFolder = "final";
    const reason = flagged[b.folder];
    if (reason === "wrong-logo") flagFolder = "flagged_wrong_logo";
    else if (reason === "needs-upscaling") flagFolder = "flagged_needs_upscaling";
    else if (reason === "wrong-colour") flagFolder = "flagged_wrong_colour";
    else if (reason === "other") flagFolder = "flagged_other";

    const safeName = b.name.replace(/[^a-zA-Z0-9 _-]/g, "").replace(/\\s+/g, "_");
    const isSelSvg = selCand ? selCand.is_svg : b.is_svg;
    const ext = isSelSvg ? "svg" : "png";

    if (isSelSvg && selCand && selCand.svg_markup) {{
      // SVG with potential recolour
      let svgMarkup = selCand.svg_markup;
      const rcHex = logoRecolours[b.folder];
      if (rcHex) {{
        svgMarkup = _applyRecolour(svgMarkup, rcHex);
      }}
      zip.file(`${{flagFolder}}/${{safeName}}.${{ext}}`, svgMarkup);
    }} else if (selCand && selCand.file) {{
      // PNG - fetch from candidates folder
      try {{
        const resp = await fetch(b.folder + "/" + selCand.file);
        const blob = await resp.blob();
        zip.file(`${{flagFolder}}/${{safeName}}.${{ext}}`, blob);
      }} catch (e) {{
        console.warn(`Failed to fetch ${{selCand.file}}:`, e);
      }}
    }}
  }}

  const content = await zip.generateAsync({{type:"blob"}});
  const a = document.createElement("a"); a.href = URL.createObjectURL(content); a.download = "approved_brand_assets.zip"; a.click();
  document.getElementById("exportStatus").textContent = `Exported ${{data.length}} brands (ZIP with logos + data)`;
}}

// ─── EVENT LISTENERS ───
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

    # ── Logo sourcing tiers ────────────────────────────────────────────────────
    tier_funcs = [
        (0, "CSV provided",  lambda: None),  # placeholder, handled below
        (1, "Brandfetch",    lambda: tier1_brandfetch(brand_name, website)),
        (2, "Website scrape",lambda: tier2_website_scrape(website)),
        (2, "Playwright SPA",lambda: tier2b_playwright_scrape(website)),  # SPA fallback
        (3, "Wikimedia",     lambda: tier3_wikimedia(brand_name)),
        (4, "Favicon",       lambda: tier4_google_favicon(website)),
        (5, "DuckDuckGo",    lambda: tier5_duckduckgo(brand_name)),
        (6, "Gilbarbara",    lambda: tier6_gilbarbara(brand_name)),
        (7, "Seeklogo",      lambda: tier7_seeklogo(brand_name)),
        (8, "Simple Icons",  lambda: tier8_simple_icons(brand_name)),
    ]

    logo_data = None
    logo_candidates = []  # list of {tier, tier_name, image, source, url, is_svg, svg_data, confidence}
    theme_color = None

    # ── TIER 0: Use pre-existing logo URL from CSV ───────────────────────────
    if existing_logo_url:
        img, is_svg, svg_raw = _fetch_image(existing_logo_url)
        if img:
            cand = {
                "tier": 0, "tier_name": "CSV provided",
                "source": "csv:provided",
                "image": img, "is_svg": is_svg,
                "url": existing_logo_url, "confidence": 0.95,
            }
            if svg_raw:
                cand["svg_data"] = svg_raw
            logo_candidates.append(cand)

    # ── TIERS 1-8 ────────────────────────────────────────────────────────────
    for tier_num, tier_name, tier_fn in tier_funcs[1:]:
        if len(logo_candidates) >= CANDIDATE_CAP:
            break  # hard cap on candidates
        try:
            td = tier_fn()
        except Exception as e:
            log.debug(f"[tier{tier_num}] {tier_name} error: {e}")
            td = None
        if not td:
            # Debug: tier returned nothing
            pass  # individual tier functions already print warnings
        if td:
            # A tier can return a single dict or a list of dicts
            td_list = td if isinstance(td, list) else [td]
            for td_item in td_list:
                cand = {
                    "tier": tier_num, "tier_name": tier_name,
                    "source": td_item.get("source", tier_name),
                    "image": td_item["image"], "is_svg": td_item.get("is_svg", False),
                    "url": td_item.get("url"), "confidence": td_item.get("confidence", 0.5),
                }
                if td_item.get("svg_data"):
                    cand["svg_data"] = td_item["svg_data"]
                if td_item.get("theme_color"):
                    theme_color = td_item["theme_color"]
                if td_item.get("si_colour"):
                    cand["si_colour"] = td_item["si_colour"]
                logo_candidates.append(cand)

    # ── Pick suggested primary by priority ──────────────────────────────────
    if logo_candidates:
        # 1. Highest-confidence SVG candidate
        # 2. Highest-confidence raster with transparency
        # 3. Highest-confidence raster without transparency
        # 4. Whatever was found
        svg_candidates = [c for c in logo_candidates if c.get("is_svg")]
        rasters = [c for c in logo_candidates if not c.get("is_svg") and c["image"]]
        raster_with_alpha = [c for c in rasters if has_transparency(c["image"])]
        raster_without_alpha = [c for c in rasters if not has_transparency(c["image"])]

        if svg_candidates:
            logo_data = max(svg_candidates, key=lambda x: x.get("confidence", 0))
        elif raster_with_alpha:
            logo_data = max(raster_with_alpha, key=lambda x: x.get("confidence", 0))
        elif raster_without_alpha:
            logo_data = max(raster_without_alpha, key=lambda x: x.get("confidence", 0))
        else:
            logo_data = logo_candidates[0]
        result["source_tier"] = logo_data["tier"]

    if not logo_data:
        result["errors"].append("No logo found in any tier")
        return result

    result["logo_candidates_count"] = len(logo_candidates)

    # ── Record source info ───────────────────────────────────────────────────
    raw_img = logo_data["image"]
    result["logo_source"] = logo_data["source"]
    result["logo_url"] = logo_data.get("url")
    result["is_svg"] = logo_data.get("is_svg", False)

    # For SVG primaries, temp-rasterize for analysis only
    if result["is_svg"] and raw_img is None:
        svg_bytes = logo_data.get("svg_data")
        if svg_bytes:
            raw_img = _svg_to_pil(svg_bytes, TARGET_SIZE)
            if not raw_img:
                result["errors"].append("Could not rasterize SVG for analysis")
                return result

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

    # Save raw image only if we have a raster primary
    if logo_data["image"]:
        logo_data["image"].save(brand_dir / "logo_raw.png")

    # Save SVG if we have it (for SVG primaries, also save as logo.svg)
    if logo_data.get("svg_data"):
        squared_svg = make_svg_square(logo_data["svg_data"])
        (brand_dir / "logo.svg").write_bytes(squared_svg)

    # Save all logo candidates to candidates/ subfolder
    candidates_dir = brand_dir / "candidates"
    candidates_dir.mkdir(exist_ok=True)

    saved_candidates = []
    for idx, cand in enumerate(logo_candidates):
        # Sanitize source name for filename
        source_slug = re.sub(r'[^a-z0-9_\-]', '_', cand["source"].lower())
        is_svg = cand.get("is_svg", False)

        cand_info = {
            "index": idx,
            "tier": cand["tier"],
            "tier_name": cand["tier_name"],
            "source": cand["source"],
            "url": cand.get("url", ""),
            "is_svg": is_svg,
            "confidence": cand.get("confidence", 0.5),
            "is_selected": (cand is logo_data),
        }

        # Save candidate file
        if is_svg and cand.get("svg_data"):
            # SVG candidate: apply make_svg_square() and save
            svg_data = cand["svg_data"]
            squared_svg = make_svg_square(svg_data)
            filename = f"{idx:02d}_{source_slug}.svg"
            filepath = candidates_dir / filename
            filepath.write_bytes(squared_svg)
            cand_info["file"] = f"candidates/{filename}"
            cand_info["size"] = "vector"

            # Create thumbnail by rasterizing the SVG
            thumb_img = _svg_to_pil(squared_svg, 400)
            if thumb_img:
                buf = BytesIO()
                thumb_img.save(buf, format="PNG")
                cand_info["thumb_b64"] = base64.b64encode(buf.getvalue()).decode()
        elif cand.get("image"):
            # Raster candidate: apply process_logo() and save
            raster_img = cand["image"].copy()
            processed_raster = process_logo(raster_img)
            filename = f"{idx:02d}_{source_slug}.png"
            filepath = candidates_dir / filename
            processed_raster.save(filepath)
            cand_info["file"] = f"candidates/{filename}"
            cand_info["size"] = f"{processed_raster.width}x{processed_raster.height}"

            # Create thumbnail
            thumb = processed_raster.copy()
            thumb.thumbnail((400, 400), Image.LANCZOS)
            if thumb.mode != "RGBA":
                thumb = thumb.convert("RGBA")
            buf = BytesIO()
            thumb.save(buf, format="PNG")
            cand_info["thumb_b64"] = base64.b64encode(buf.getvalue()).decode()

        saved_candidates.append(cand_info)

    result["logo_candidates"] = saved_candidates

    with open(brand_dir / "meta.json", "w") as f:
        json.dump({k: v for k, v in result.items() if k != "image"}, f, indent=2, cls=SafeEncoder)

    return result


def main():
    parser = argparse.ArgumentParser(description="Brand Asset Pipeline v6")
    parser.add_argument("--input", required=True, help="Input CSV file")
    parser.add_argument("--sample", type=int, default=0, help="Number of brands to sample (0=all)")
    parser.add_argument("--output", default="./brand_assets", help="Output directory")
    parser.add_argument("--rembg-model", default="u2net",
                        choices=["u2net", "u2net_human_seg", "isnet-general-use"],
                        help="Background removal model (default: u2net)")
    parser.add_argument("--alpha-matting", action="store_true",
                        help="Enable alpha matting for cleaner edges (slower)")
    parser.add_argument("--threads", type=int, default=1,
                        help="Number of parallel threads (default: 1, recommended: 4)")
    parser.add_argument("--log-level", default="info", choices=["info", "debug"],
                        help="Log level: info (clean terminal) or debug (verbose)")
    args = parser.parse_args()

    global OUT_DIR, REMBG_MODEL, ALPHA_MATTING
    OUT_DIR = Path(args.output)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REMBG_MODEL = args.rembg_model
    ALPHA_MATTING = args.alpha_matting

    # Setup logging — file handler needs the output directory
    _setup_file_logging(OUT_DIR, args.log_level)

    # Read CSV
    with open(args.input) as f:
        rows = list(csv.DictReader(f))

    if args.sample and args.sample < len(rows):
        import random
        random.seed(42)
        rows = random.sample(rows, args.sample)

    print(f"\n{'='*60}")
    print(f"  Brand Asset Pipeline v6 — Processing {len(rows)} brands")
    print(f"  Output:  {OUT_DIR.absolute()}")
    print(f"  Model:   {REMBG_MODEL}  |  Alpha matting: {'ON' if ALPHA_MATTING else 'OFF'}")
    svg_status = "\u2705 cairosvg" if HAS_CAIROSVG else "\u26a0\ufe0f  cairosvg NOT installed"
    pw_status = "\u2705 playwright" if HAS_PLAYWRIGHT else "\u26a0\ufe0f  playwright NOT installed"
    print(f"  Deps:    {svg_status}  |  {pw_status}")
    print(f"  Mode:    All tiers, up to {CANDIDATE_CAP} candidates per brand")
    if args.threads > 1:
        print(f"  Threads: {args.threads}")
    print(f"  Log:     {OUT_DIR / 'pipeline.log'}")
    print(f"{'='*60}\n")

    results = []
    tier_counts = Counter()  # counts how many brands each tier was the PRIMARY source for
    tier_candidate_counts = Counter()  # counts total candidates from each tier
    status_counts = Counter()

    # Auto-detect CSV column names
    col_name, col_site, col_logo, col_color = "", "", "", ""
    if rows:
        cols = list(rows[0].keys())
        col_name = next((c for c in cols if c.lower() in ("brand_name", "name", "brand")), cols[0])
        col_site = next((c for c in cols if c.lower() in ("business_website", "url", "website", "site")), "")
        col_logo = next((c for c in cols if c.lower() in ("logo", "logo_url", "image", "image_url")), "")
        col_color = next((c for c in cols if c.lower() in ("color", "colour", "brand_colour", "hex")), "")
        print(f"  CSV columns: name={col_name}, site={col_site or 'N/A'}, logo={col_logo or 'N/A'}, color={col_color or 'N/A'}\n")

    # Build work items
    work_items = []
    for i, row in enumerate(rows):
        name = row.get(col_name, "").strip()
        site = row.get(col_site, "").strip() if col_site else ""
        existing_logo_url = row.get(col_logo, "").strip() if col_logo else ""
        existing_color = row.get(col_color, "").strip() if col_color else ""
        if name:
            work_items.append((i, name, site, existing_logo_url, existing_color))

    import threading
    print_lock = threading.Lock()
    completed = [0]

    def _process_one(item):
        idx, name, site, logo_url, color = item
        result = process_brand(name, site, logo_url, color)
        with print_lock:
            completed[0] += 1
            n = completed[0]
            cand_count = result.get("logo_candidates_count", 0)
            if result["status"] == "success":
                # Count PNG vs SVG candidates
                cands = result.get("logo_candidates", [])
                svg_c = sum(1 for c in cands if c.get("is_svg"))
                png_c = len(cands) - svg_c
                emoji = "\u2705" if cand_count >= 3 else ("\u26a0\ufe0f " if cand_count >= 1 else "\U0001f534")
                print(f"[{n:3d}/{len(work_items)}] {name:35s} {emoji}  {cand_count} candidates ({png_c} PNG, {svg_c} SVG)")
            else:
                print(f"[{n:3d}/{len(work_items)}] {name:35s} \u274c  {result['errors'][:1]}")
        return result

    if args.threads > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as pool:
            results = list(pool.map(_process_one, work_items))
    else:
        results = []
        for item in work_items:
            results.append(_process_one(item))
            time.sleep(0.2)

    for result in results:
        status_counts[result["status"]] += 1
        if result.get("source_tier") is not None:
            tier_counts[f"tier{result['source_tier']}"] += 1
        # Count all candidates per tier
        for cand in result.get("logo_candidates", []):
            tier_candidate_counts[f"tier{cand['tier']}"] += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    tier_names = {
        0: "CSV provided", 1: "Brandfetch", 2: "Website scrape",
        3: "Wikimedia", 4: "Favicon", 5: "DuckDuckGo",
        6: "Gilbarbara", 7: "Seeklogo", 8: "Simple Icons",
    }
    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Total:      {len(results)}")
    print(f"  Success:    {status_counts.get('success', 0)}")
    print(f"  Failed:     {status_counts.get('failed', 0)}")

    total_candidates = sum(tier_candidate_counts.values())
    print(f"  Total candidates sourced: {total_candidates}")

    print(f"\n  Source effectiveness (primary selected / total candidates from tier):")
    print(f"  {'Tier':<6} {'Source':<18} {'Primary':>7}  {'Cands':>7}  {'Bar':<20}")
    print(f"  {'-'*70}")
    for t in range(9):
        primary = tier_counts.get(f"tier{t}", 0)
        cands = tier_candidate_counts.get(f"tier{t}", 0)
        name = tier_names.get(t, f"Tier {t}")
        bar = "\u2588" * min(cands, 40)
        print(f"  T{t:<5} {name:<18} {primary:>7}  {cands:>7}  {bar:<20}")

    svg_count = sum(1 for r in results if r.get("is_svg"))
    undersize_count = sum(1 for r in results if r.get("undersize"))
    bg_removed = sum(1 for r in results if r.get("bg_removed"))
    high_risk = [r for r in results if r.get("blending_risk") == "HIGH"]
    logo_issues = [r for r in results if r.get("logo_issues")]

    print(f"\n  Quality indicators:")
    print(f"    SVG primary selected: {svg_count}")
    print(f"    Undersize (<500px):   {undersize_count}")
    print(f"    BG removed (rembg):   {bg_removed}")
    print(f"    High blending risk:   {len(high_risk)}")
    print(f"    Logo issues flagged:  {len(logo_issues)}")

    # Category breakdown
    cat_counts = Counter(r.get("category", "Uncategorized") for r in results if r["status"] == "success")
    if cat_counts:
        print(f"\n  Category breakdown:")
        for cat, cnt in cat_counts.most_common():
            print(f"    {cat:<24} {cnt}")

    # Save summary JSON
    with open(OUT_DIR / "pipeline_summary.json", "w") as f:
        json.dump(results, f, indent=2, cls=SafeEncoder, default=str)

    # Save review CSV
    with open(OUT_DIR / "review.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "brand_name", "status", "source_tier", "logo_source", "is_svg",
            "brand_colour", "colour_source", "colour_candidates", "blending_risk",
            "has_transparency", "bg_removed", "undersize", "original_size",
            "confidence", "logo_url", "logo_candidates_count",
        ])
        w.writeheader()
        for r in results:
            row = {k: r.get(k) for k in w.fieldnames}
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
    print(f"  \U0001f4d3 Debug log:       {OUT_DIR / 'pipeline.log'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
