"""
Brand Asset Pipeline — processors package
=========================================

Three modules:
- raster_processor: PIL-based padding/squaring + alpha-preserving monochromize
- svg_processor:    lxml-based viewBox normalization + DOM-based recolour
- upscaler:         Upscayl / realesrgan-ncnn-vulkan subprocess wrapper

All public functions are pure (input → output) where possible. Logging via the
parent pipeline's "pipeline" logger so warnings/errors flow into pipeline.log.
"""

from .raster_processor import (
    pad_to_square,
    monochromize,
    get_dimensions,
    needs_upscale,
)
from .svg_processor import (
    normalize_svg,
    recolour_svg,
    is_svg,
)
from .upscaler import (
    upscale_if_needed,
    is_upscayl_available,
    auto_detect_upscayl_bin,
)

__all__ = [
    "pad_to_square",
    "monochromize",
    "get_dimensions",
    "needs_upscale",
    "normalize_svg",
    "recolour_svg",
    "is_svg",
    "upscale_if_needed",
    "is_upscayl_available",
    "auto_detect_upscayl_bin",
]
