"""
svg_processor.py
================

SVG-specific operations:
  - normalize_svg: trim viewBox to content + add 12px padding + make square
  - recolour_svg:  DOM-based recolour (attrs, inline styles, <style> blocks, gradient stops)
  - is_svg:        sniff bytes to identify SVG content

Uses lxml for XML manipulation. Falls back to ElementTree when lxml is missing
but a few features (CSS-in-style-block recolour) require lxml. Uses cairosvg
to compute the actual rendered content bounding box, which is more reliable
than walking the SVG DOM ourselves (transforms, clip paths, masks all complicate
geometric bbox calculation).
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Tuple

import numpy as np
from PIL import Image

log = logging.getLogger("pipeline")

# lxml is preferred (handles XML namespaces and pretty-printing well).
# Fall back to stdlib ElementTree if not installed.
try:
    from lxml import etree as LET
    HAS_LXML = True
except ImportError:
    HAS_LXML = False
    from xml.etree import ElementTree as LET  # type: ignore

try:
    import cairosvg
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False


SVG_NS = "http://www.w3.org/2000/svg"


# ─── DETECTION ───────────────────────────────────────────────────────────────

def is_svg(data: bytes) -> bool:
    """Quick sniff: True if the bytes look like SVG content."""
    if not data:
        return False
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (b"<?xml" in head[:100] and b"<svg" in data[:2048].lower())


# ─── PARSING HELPERS ─────────────────────────────────────────────────────────

def _parse(svg_bytes: bytes):
    """Parse SVG bytes → (root, used_lxml). Strips BOMs and handles missing namespace."""
    if HAS_LXML:
        parser = LET.XMLParser(remove_blank_text=False, recover=True)
        root = LET.fromstring(svg_bytes, parser)
    else:
        root = LET.fromstring(svg_bytes)
    return root


def _serialize(root) -> bytes:
    """Serialize root → bytes with proper XML declaration and SVG namespace."""
    if HAS_LXML:
        return LET.tostring(root, xml_declaration=False, encoding="utf-8")
    else:
        # ElementTree adds ns0: prefix sometimes — register first
        LET.register_namespace("", SVG_NS)
        return LET.tostring(root, encoding="utf-8")


def _strip_namespace(tag: str) -> str:
    """svg:rect → rect (lxml-style namespaced tags)."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# ─── CONTENT BOUNDING BOX ────────────────────────────────────────────────────

def _content_bbox_via_cairosvg(svg_bytes: bytes, render_size: int = 1024) -> Tuple[float, float, float, float] | None:
    """
    Render the SVG to a high-res PNG via cairosvg, find non-transparent bbox,
    return as fractions (x_pct, y_pct, w_pct, h_pct) of the render size.

    Returns None if rendering fails or no content.
    """
    if not HAS_CAIROSVG:
        return None
    try:
        png_bytes = cairosvg.svg2png(
            bytestring=svg_bytes,
            output_width=render_size,
            output_height=render_size,
        )
        img = Image.open(BytesIO(png_bytes)).convert("RGBA")
        arr = np.array(img)
        alpha = arr[:, :, 3]
        rows = np.any(alpha > 5, axis=1)
        cols = np.any(alpha > 5, axis=0)
        if not rows.any() or not cols.any():
            return None
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        h, w = arr.shape[:2]
        return (cmin / w, rmin / h, (cmax - cmin + 1) / w, (rmax - rmin + 1) / h)
    except Exception as e:
        log.debug(f"[svg-bbox] cairosvg render failed: {e}")
        return None


def _get_existing_viewbox(root) -> Tuple[float, float, float, float] | None:
    vb = root.get("viewBox")
    if not vb:
        return None
    try:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) != 4:
            return None
        return tuple(float(p) for p in parts)  # type: ignore
    except Exception:
        return None


# ─── NORMALIZE (square viewBox + padding) ────────────────────────────────────

def normalize_svg(
    svg_bytes: bytes,
    padding_px: int = 12,
    canvas_size: int = 512,
) -> bytes:
    """
    Compute content bbox, expand to square + padding, rewrite viewBox.

    Strategy:
      1. Render SVG to PNG via cairosvg, find rendered content bbox.
      2. Map back to SVG user units using the existing viewBox (or width/height).
      3. Expand bbox to a square (max dim).
      4. Add padding equivalent to padding_px of an output canvas of canvas_size.
      5. Set viewBox to the new rect; remove width/height so SVG scales fluidly.

    If cairosvg is unavailable or rendering fails, falls back to making the
    existing viewBox square (no content-aware padding). The result is still
    safer than the original.
    """
    if not svg_bytes:
        return svg_bytes

    try:
        root = _parse(svg_bytes)
    except Exception as e:
        log.debug(f"[normalize_svg] Parse failed: {e}; returning original")
        return svg_bytes

    # Existing viewBox or fall back to width/height
    vb = _get_existing_viewbox(root)
    if vb is None:
        w_attr = root.get("width") or "100"
        h_attr = root.get("height") or "100"
        try:
            w_val = float(re.sub(r"[^\d.]", "", w_attr) or 100)
            h_val = float(re.sub(r"[^\d.]", "", h_attr) or 100)
            vb = (0.0, 0.0, w_val, h_val)
        except Exception:
            vb = (0.0, 0.0, 100.0, 100.0)

    vx, vy, vw, vh = vb
    if vw <= 0 or vh <= 0:
        return svg_bytes

    # Try content-aware bbox via cairosvg
    bbox_pct = _content_bbox_via_cairosvg(svg_bytes)
    if bbox_pct:
        bx_pct, by_pct, bw_pct, bh_pct = bbox_pct
        # Map percentages back to user-unit space using current viewBox
        cx = vx + bx_pct * vw
        cy = vy + by_pct * vh
        cw = bw_pct * vw
        ch = bh_pct * vh
    else:
        # Fallback: assume content fills the viewBox
        cx, cy, cw, ch = vx, vy, vw, vh

    # Make square (use max dim)
    side = max(cw, ch)
    # Centre the smaller dim
    cx -= (side - cw) / 2
    cy -= (side - ch) / 2

    # Add padding — convert padding_px on a canvas_size canvas to user units
    # padding_in_user_units / side = padding_px / (canvas_size - 2*padding_px)
    # → padding_in_user_units = side * padding_px / (canvas_size - 2*padding_px)
    inner = max(canvas_size - 2 * padding_px, 1)
    pad_user = side * (padding_px / inner)

    new_x = cx - pad_user
    new_y = cy - pad_user
    new_side = side + 2 * pad_user

    # Set new viewBox, drop hard-coded width/height so SVG scales
    root.set("viewBox", f"{new_x:.4f} {new_y:.4f} {new_side:.4f} {new_side:.4f}")
    if "width" in root.attrib:
        del root.attrib["width"]
    if "height" in root.attrib:
        del root.attrib["height"]
    # Add intrinsic aspect ratio attribute for browsers
    root.set("preserveAspectRatio", "xMidYMid meet")

    return _serialize(root)


# ─── DOM RECOLOUR ────────────────────────────────────────────────────────────

# Regex used inside text content of <style> blocks (CSS rules)
_CSS_FILL_RE = re.compile(r"\b(fill)\s*:\s*([^;}\s]+)", re.IGNORECASE)
_CSS_STROKE_RE = re.compile(r"\b(stroke)\s*:\s*([^;}\s]+)", re.IGNORECASE)
_CSS_STOP_RE = re.compile(r"\b(stop-color)\s*:\s*([^;}\s]+)", re.IGNORECASE)


def _is_paintable(value: str | None) -> bool:
    """True if a fill/stroke value should be replaced (i.e. it's a colour, not 'none' or a gradient ref)."""
    if not value:
        return False
    v = value.strip().lower()
    if v in ("none", "transparent", "inherit", "currentcolor", "context-fill", "context-stroke"):
        return True if v == "currentcolor" else False
    if v.startswith("url("):
        return False
    return True


def _replace_inline_style(style: str, hex_colour: str) -> str:
    """Replace fill:, stroke:, stop-color: in an inline style attribute."""
    def _sub(match):
        prop, val = match.group(1), match.group(2).strip()
        if val.lower() in ("none", "inherit") or val.startswith("url("):
            return match.group(0)
        return f"{prop}: {hex_colour}"
    style = _CSS_FILL_RE.sub(_sub, style)
    style = _CSS_STROKE_RE.sub(_sub, style)
    style = _CSS_STOP_RE.sub(_sub, style)
    return style


def _replace_style_block(css: str, hex_colour: str) -> str:
    """Replace fill/stroke/stop-color values inside a <style> block's CSS text."""
    def _sub(match):
        prop, val = match.group(1), match.group(2).strip()
        if val.lower() in ("none", "inherit") or val.startswith("url("):
            return match.group(0)
        return f"{prop}: {hex_colour}"
    css = _CSS_FILL_RE.sub(_sub, css)
    css = _CSS_STROKE_RE.sub(_sub, css)
    css = _CSS_STOP_RE.sub(_sub, css)
    return css


def recolour_svg(svg_bytes: bytes, hex_colour: str) -> bytes:
    """
    Walk the SVG DOM and replace every colour-bearing fill/stroke/stop-color with hex_colour.

    Handles:
      - fill="..." and stroke="..." attributes
      - style="fill: ...; stroke: ..." inline style attributes
      - <style> CSS blocks (fill, stroke, stop-color rules)
      - <stop stop-color="..."> inside gradients

    Skips:
      - fill="none", stroke="none"
      - fill="url(#grad)" (gradient references — handled via stop-color recolour)

    Returns modified SVG bytes. On parse failure, returns original.
    """
    if not svg_bytes or not hex_colour:
        return svg_bytes

    # Validate hex
    if not re.match(r"^#[0-9a-fA-F]{6}$", hex_colour):
        log.warning(f"[recolour_svg] Invalid hex {hex_colour!r}, skipping")
        return svg_bytes

    try:
        root = _parse(svg_bytes)
    except Exception as e:
        log.debug(f"[recolour_svg] Parse failed: {e}; returning original")
        return svg_bytes

    # Walk every element
    iterator = root.iter() if HAS_LXML else root.iter()
    for elem in iterator:
        tag = _strip_namespace(elem.tag).lower() if isinstance(elem.tag, str) else ""

        # 1. fill / stroke attributes
        for attr in ("fill", "stroke"):
            val = elem.get(attr)
            if val is not None and _is_paintable(val):
                elem.set(attr, hex_colour)

        # 2. stop-color attribute (gradient stops)
        if tag == "stop":
            sc = elem.get("stop-color")
            if sc is not None and _is_paintable(sc):
                elem.set("stop-color", hex_colour)

        # 3. inline style attribute
        style = elem.get("style")
        if style:
            new_style = _replace_inline_style(style, hex_colour)
            if new_style != style:
                elem.set("style", new_style)

        # 4. <style> block CSS text
        if tag == "style" and elem.text:
            new_css = _replace_style_block(elem.text, hex_colour)
            if new_css != elem.text:
                elem.text = new_css

    return _serialize(root)
