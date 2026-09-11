"""Page-fitting and render helpers for the Windows GDI print path.

Everything in this module is deliberately free of win32/PIL/pypdfium2
imports so the geometry can be unit-tested on any platform.
"""

import os
from typing import Tuple

from utils.logger import logger

# Rendering a page to a bitmap costs roughly (dpi/72)^2 * page_area bytes, so a
# 1200 dpi laser driver would ask for ~280 MB per A4 page. Cap the render
# resolution at 300 dpi (plenty for text and photos on any office device) and
# let power users override it with PRINTER_AI_RENDER_DPI when they really want
# the extra detail and have the memory for it.
DEFAULT_MAX_RENDER_DPI = 300
MIN_RENDER_DPI = 36
MAX_RENDER_DPI = 1200

RENDER_DPI_ENV = "PRINTER_AI_RENDER_DPI"


def resolve_render_dpi(device_dpi: int, max_dpi: int = DEFAULT_MAX_RENDER_DPI) -> int:
    """Pick the rasterisation DPI for a printer.

    Args:
        device_dpi: The printer's LOGPIXELSX (device resolution).
        max_dpi: Memory guard - never render above this unless overridden.

    Returns:
        int: DPI to hand to the PDF rasteriser.
    """
    override = os.environ.get(RENDER_DPI_ENV)
    if override:
        try:
            dpi = int(str(override).strip())
        except (TypeError, ValueError):
            logger.error(f"[resolve_render_dpi] ignoring invalid {RENDER_DPI_ENV}={override!r}")
        else:
            return max(MIN_RENDER_DPI, min(MAX_RENDER_DPI, dpi))

    try:
        dpi = int(device_dpi)
    except (TypeError, ValueError):
        dpi = max_dpi
    if dpi <= 0:
        dpi = max_dpi
    return max(MIN_RENDER_DPI, min(max_dpi, dpi))


def fit_rect(
    src_w: int,
    src_h: int,
    area_w: int,
    area_h: int,
    off_x: int = 0,
    off_y: int = 0,
) -> Tuple[int, int, int, int]:
    """Fit a source bitmap into a target area, preserving aspect and centering.

    Args:
        src_w, src_h: Rendered page size in pixels.
        area_w, area_h: Target area (device units).
        off_x, off_y: Origin of the target area in the DC's coordinate system.

    Returns:
        (x0, y0, x1, y1) destination rectangle in device units.

    Raises:
        ValueError: if any dimension is not positive.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"invalid source size: {src_w}x{src_h}")
    if area_w <= 0 or area_h <= 0:
        raise ValueError(f"invalid target area: {area_w}x{area_h}")

    scale = min(area_w / src_w, area_h / src_h)
    width = max(1, int(round(src_w * scale)))
    height = max(1, int(round(src_h * scale)))

    # Never spill out of the area because of rounding
    width = min(width, area_w)
    height = min(height, area_h)

    x0 = off_x + (area_w - width) // 2
    y0 = off_y + (area_h - height) // 2
    return (x0, y0, x0 + width, y0 + height)


def page_order(page_count: int, copies: int, collate: bool):
    """Page indices to draw for client-side copies.

    Collated copies repeat the whole document (1,2,3,1,2,3); uncollated
    copies repeat each page in place (1,1,2,2,3,3).

    Args:
        page_count: Number of pages in the document.
        copies: Number of copies to produce client-side (>= 1).
        collate: True for collated output.

    Returns:
        list[int]: zero-based page indices in draw order.
    """
    copies = max(1, int(copies or 1))
    if copies == 1:
        return list(range(page_count))
    if collate:
        return [i for _ in range(copies) for i in range(page_count)]
    return [i for i in range(page_count) for _ in range(copies)]
