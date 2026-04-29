"""
raster_processor.py
===================

PIL/NumPy operations for raster logos:
  - pad_to_square: trim transparent borders, centre on a square canvas with N px padding
  - monochromize:  alpha-preserving RGB replacement (lossless silhouette)
  - needs_upscale: True if min(w,h) < threshold

All operations are alpha-aware. RGB-only inputs (e.g. JPEG) are converted to RGBA.
For RGB inputs without a clear alpha channel, the caller should run rembg first.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Tuple, Union

import numpy as np
from PIL import Image

log = logging.getLogger("pipeline")


# ─── DIMENSIONS ──────────────────────────────────────────────────────────────

def get_dimensions(img: Union[Image.Image, str, Path]) -> Tuple[int, int]:
    """Return (width, height) for a PIL image or a file path."""
    if isinstance(img, (str, Path)):
        with Image.open(img) as opened:
            return opened.size
    return img.size


def needs_upscale(img: Union[Image.Image, str, Path], threshold: int = 500) -> bool:
    """True if min(width, height) < threshold."""
    w, h = get_dimensions(img)
    return min(w, h) < threshold


# ─── PADDING & SQUARING ──────────────────────────────────────────────────────

def _content_bbox_alpha(arr: np.ndarray, alpha_threshold: int = 10) -> Tuple[int, int, int, int] | None:
    """
    Find the bounding box of non-transparent content in an RGBA array.
    Returns (left, top, right, bottom) or None if no content.
    """
    alpha = arr[:, :, 3]
    rows = np.any(alpha > alpha_threshold, axis=1)
    cols = np.any(alpha > alpha_threshold, axis=0)
    if not rows.any() or not cols.any():
        return None
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    return int(cmin), int(rmin), int(cmax) + 1, int(rmax) + 1


def pad_to_square(
    img: Image.Image,
    padding_px: int = 12,
    canvas_size: int | None = None,
    background: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Image.Image:
    """
    Trim transparent borders, then centre the content on a square canvas.

    - padding_px: pixels of breathing room between content edge and canvas edge
    - canvas_size: if given, output is exactly this size (px) and content scales
                   to fit canvas - 2*padding_px. If None, canvas auto-sizes
                   to max(content_w, content_h) + 2*padding_px.
    - background: RGBA fill for the canvas (default fully transparent)

    Returns RGBA image.
    """
    img = img.convert("RGBA")
    arr = np.array(img)

    bbox = _content_bbox_alpha(arr)
    if bbox is None:
        log.debug("[pad_to_square] No content found (image fully transparent), returning original")
        return img

    cropped = img.crop(bbox)
    cw, ch = cropped.size
    if cw < 1 or ch < 1:
        return img

    if canvas_size is not None:
        # Fixed output size — scale content to fit (canvas_size - 2*padding_px)
        inner = max(canvas_size - 2 * padding_px, 1)
        scale = min(inner / cw, inner / ch)
        new_w = max(int(round(cw * scale)), 1)
        new_h = max(int(round(ch * scale)), 1)
        # Use LANCZOS for downscale, NEAREST_LIKE for upscale via LANCZOS too (no quality loss vs upscale separately)
        cropped = cropped.resize((new_w, new_h), Image.LANCZOS)
        cw, ch = new_w, new_h
        size = canvas_size
    else:
        # Auto-size canvas
        size = max(cw, ch) + 2 * padding_px

    canvas = Image.new("RGBA", (size, size), background)
    paste_x = (size - cw) // 2
    paste_y = (size - ch) // 2
    canvas.paste(cropped, (paste_x, paste_y), cropped)
    return canvas


# ─── MONOCHROMIZATION (lossless) ─────────────────────────────────────────────

def monochromize(img: Image.Image, colour: str) -> Image.Image:
    """
    Replace all RGB values with `colour` while preserving the alpha channel exactly.

    Lossless on edges — alpha gradients (anti-aliasing) are kept intact.

    colour:
      - "black"         → (0, 0, 0)
      - "white"         → (255, 255, 255)
      - "#RRGGBB" hex   → custom (kept for flexibility, not a primary use case)
      - "none" / falsy  → returns image unchanged

    Requires the input to have a meaningful alpha channel. RGB inputs are
    converted to RGBA (assumes opaque) — caller should run rembg first for
    JPEGs with white backgrounds.
    """
    if not colour or colour.lower() == "none" or colour.lower() == "original":
        return img

    img = img.convert("RGBA")
    arr = np.array(img)

    if colour.lower() == "black":
        target = (0, 0, 0)
    elif colour.lower() == "white":
        target = (255, 255, 255)
    elif colour.startswith("#") and len(colour) == 7:
        try:
            target = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
        except ValueError:
            log.warning(f"[monochromize] Invalid hex {colour!r}, skipping")
            return img
    else:
        log.warning(f"[monochromize] Unsupported colour {colour!r}, skipping")
        return img

    # Vectorised: replace R, G, B; leave alpha untouched
    arr[:, :, 0] = target[0]
    arr[:, :, 1] = target[1]
    arr[:, :, 2] = target[2]
    return Image.fromarray(arr, mode="RGBA")


# ─── BYTES <-> IMAGE HELPERS ─────────────────────────────────────────────────

def img_to_png_bytes(img: Image.Image) -> bytes:
    """Serialize a PIL image to PNG bytes."""
    buf = BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def bytes_to_img(data: bytes) -> Image.Image:
    """Deserialize bytes to a PIL image (RGBA)."""
    return Image.open(BytesIO(data)).convert("RGBA")
