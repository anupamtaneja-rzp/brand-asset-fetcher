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

# Models that ship with realesrgan-ncnn-vulkan
DEFAULT_MODELS = {
    "realesrgan-x4plus": "Photo content; smooths edges (not ideal for logos)",
    "realesrgan-x4plus-anime": "Line art / logos / typography — preserves crisp edges",
    "realesrnet-x4plus": "More aggressive denoising",
    "realesr-animevideov3-x4": "Animated content",
}


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
    model: str = "realesrgan-x4plus-anime",
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

    # Run upscaler
    cmd = [
        binary,
        "-i", str(input_path),
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
