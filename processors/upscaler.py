"""
upscaler.py
===========

Subprocess wrapper for Upscayl / realesrgan-ncnn-vulkan.

  - auto_detect_upscayl_bin: search common install locations
  - is_upscayl_available:    True if a usable binary is on PATH or found
  - upscale_if_needed:       take an image path, return path to upscaled
                             (or original if not needed / upscaler unavailable)

Caches outputs by content hash → repeat invocations are free.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("pipeline")


# ─── BINARY DETECTION ────────────────────────────────────────────────────────

# Common install locations to probe, in priority order
_CANDIDATE_BINARIES = [
    # Brew (universal mac)
    "realesrgan-ncnn-vulkan",
    "/opt/homebrew/bin/realesrgan-ncnn-vulkan",
    "/usr/local/bin/realesrgan-ncnn-vulkan",
    # Upscayl desktop app on macOS
    "/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin",
    # Upscayl desktop on Linux
    os.path.expanduser("~/.local/bin/upscayl-bin"),
    "/opt/Upscayl/resources/bin/upscayl-bin",
    # Windows
    r"C:\Program Files\Upscayl\resources\bin\upscayl-bin.exe",
]


def auto_detect_upscayl_bin(override: str | None = None) -> str | None:
    """
    Return path to a working upscayl/realesrgan binary, or None if not found.
    """
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        # try as command name on PATH
        found = shutil.which(override)
        if found:
            return found
        log.warning(f"[upscaler] --upscayl-bin override {override!r} not found")
        return None

    for cand in _CANDIDATE_BINARIES:
        # If just a command name, use shutil.which
        if "/" not in cand and "\\" not in cand:
            found = shutil.which(cand)
            if found:
                return found
            continue
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def is_upscayl_available(override: str | None = None) -> bool:
    return auto_detect_upscayl_bin(override) is not None


# ─── MODELS ──────────────────────────────────────────────────────────────────

# Default model names depend on the binary distribution:
#   - Upscayl app bundle: digital-art-4x, upscayl-standard-4x, upscayl-lite-4x, ...
#   - Vanilla realesrgan-ncnn-vulkan: realesrgan-x4plus, realesrgan-x4plus-anime, ...
# digital-art-4x is the right pick for logos when running through Upscayl
# (specifically tuned for line art / illustrations / typography).
DEFAULT_MODELS = {
    # Upscayl-bundled (preferred for our use case)
    "digital-art-4x":         "Upscayl: line art / logos / typography — preserves crisp edges",
    "upscayl-standard-4x":    "Upscayl: balanced general-purpose default",
    "upscayl-lite-4x":        "Upscayl: faster, slightly lower fidelity",
    "high-fidelity-4x":       "Upscayl: photographic content, max detail",
    "ultramix-balanced":      "Upscayl: mixed content",
    # Vanilla realesrgan
    "realesrgan-x4plus":      "Vanilla: photo content (smooths edges)",
    "realesrgan-x4plus-anime":"Vanilla: anime / line art",
}


def list_available_models(binary: str | None = None) -> list[str]:
    """Inspect the binary's models/ directory and return available model names."""
    bin_path = binary or auto_detect_upscayl_bin()
    if not bin_path:
        return []
    # Models live next to the binary in a 'models/' folder, OR one level up in Resources/models/
    candidates = [
        Path(bin_path).parent / "models",
        Path(bin_path).parent.parent / "models",
    ]
    for d in candidates:
        if d.is_dir():
            return sorted({p.stem for p in d.glob("*.bin")})
    return []


# ─── CACHE ───────────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cache_key(input_path: Path, scale: int, model: str) -> str:
    return f"{_file_hash(input_path)}_{scale}x_{model}"


# ─── UPSCALE ─────────────────────────────────────────────────────────────────

def upscale_if_needed(
    input_path: Path | str,
    output_path: Path | str | None = None,
    threshold: int = 500,
    scale: int = 4,
    model: str = "digital-art-4x",
    binary_override: str | None = None,
    cache_dir: Path | str | None = None,
    timeout: int = 120,
) -> Path:
    """
    Upscale image if its smaller dimension < threshold. Returns path to output.

    If upscaler unavailable, threshold not met, or upscale fails, returns the
    input path unchanged (so callers can transparently use the result).
    """
    input_path = Path(input_path)
    if not input_path.exists():
        log.warning(f"[upscaler] Input not found: {input_path}")
        return input_path

    # Check threshold via PIL (cheap)
    try:
        from PIL import Image
        with Image.open(input_path) as img:
            w, h = img.size
        if min(w, h) >= threshold:
            log.debug(f"[upscaler] {input_path.name} is {w}x{h}, above threshold {threshold}, skipping")
            return input_path
    except Exception as e:
        log.debug(f"[upscaler] PIL dimension read failed: {e}; will attempt upscale anyway")

    # Find binary
    binary = auto_detect_upscayl_bin(binary_override)
    if not binary:
        log.warning(f"[upscaler] Upscayl binary not found; skipping upscale of {input_path.name}")
        return input_path

    # Validate model — Upscayl silently outputs garbage (often all-black) when the
    # requested model isn't present. Detect this BEFORE running the binary.
    available = list_available_models(binary)
    if available and model not in available:
        # Try sensible fallbacks in order
        fallback_order = ["digital-art-4x", "upscayl-standard-4x", "upscayl-lite-4x",
                          "realesrgan-x4plus-anime", "realesrgan-x4plus"]
        chosen = next((m for m in fallback_order if m in available), None)
        if chosen:
            log.warning(f"[upscaler] Model {model!r} not found in {Path(binary).parent}; "
                        f"falling back to {chosen!r}. Available: {', '.join(available)}")
            model = chosen
        else:
            log.warning(f"[upscaler] Model {model!r} not found and no fallback available. "
                        f"Available models: {', '.join(available)}. Skipping upscale of {input_path.name}.")
            return input_path

    # Resolve output path
    if output_path is None:
        output_path = input_path.with_name(input_path.stem + f".upscaled_{scale}x.png")
    output_path = Path(output_path)

    # Cache check
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            key = _cache_key(input_path, scale, model)
            cached = cache_dir / f"{key}.png"
            if cached.exists():
                log.debug(f"[upscaler] Cache hit for {input_path.name}")
                shutil.copy(cached, output_path)
                return output_path
        except Exception as e:
            log.debug(f"[upscaler] Cache lookup failed: {e}")

    # Upscayl/realesrgan-ncnn-vulkan only accepts PNG/JPG/WEBP as input.
    # For other formats (AVIF, GIF, BMP, TIFF, ICO), convert to PNG via PIL first.
    SUPPORTED_INPUT_FMTS = {"PNG", "JPEG", "JPG", "WEBP"}
    actual_input = input_path
    converted_temp: Path | None = None
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(input_path) as probe:
            src_fmt = (probe.format or "").upper()
        if src_fmt and src_fmt not in SUPPORTED_INPUT_FMTS:
            converted_temp = input_path.with_name(input_path.stem + "._upscale_input.png")
            _PILImage.open(input_path).convert("RGBA").save(converted_temp, "PNG")
            actual_input = converted_temp
            log.debug(f"[upscaler] Converted {src_fmt} → PNG for Upscayl: {input_path.name}")
    except Exception as e:
        log.debug(f"[upscaler] Format probe/convert failed for {input_path.name}: {e}; using original")

    # Run upscaler
    cmd = [
        binary,
        "-i", str(actual_input),
        "-o", str(output_path),
        "-s", str(scale),
        "-n", model,
        "-f", "png",
    ]
    log.debug(f"[upscaler] Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                f"[upscaler] {input_path.name} failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:200]}"
            )
            return input_path
        if not output_path.exists():
            log.warning(f"[upscaler] {input_path.name}: binary returned 0 but output missing")
            return input_path

        # Save to cache
        if cache_dir:
            try:
                key = _cache_key(input_path, scale, model)
                cached = cache_dir / f"{key}.png"
                shutil.copy(output_path, cached)
            except Exception as e:
                log.debug(f"[upscaler] Cache write failed: {e}")

        log.debug(f"[upscaler] Upscaled {input_path.name} → {output_path.name}")
        return output_path
    except subprocess.TimeoutExpired:
        log.warning(f"[upscaler] Timeout ({timeout}s) on {input_path.name}")
        return input_path
    except FileNotFoundError:
        log.warning(f"[upscaler] Binary {binary!r} not found at runtime")
        return input_path
    except Exception as e:
        log.warning(f"[upscaler] Unexpected error on {input_path.name}: {e}")
        return input_path
    finally:
        # Clean up temp PNG conversion file if we made one
        if converted_temp is not None:
            try:
                converted_temp.unlink()
            except Exception:
                pass
