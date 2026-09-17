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


# A per-inch cap is not enough on its own: a 36x48 inch poster at 300 dpi is
# still 155 megapixels (~620 MB of RGBA). Bound the whole bitmap too.
DEFAULT_MAX_RENDER_MEGAPIXELS = 50.0


def device_units_to_inches(units: int, device_dpi: int) -> float:
    """Convert a GetDeviceCaps length (device pixels) to inches; 0 if unknown."""
    try:
        units = float(units)
        device_dpi = float(device_dpi)
    except (TypeError, ValueError):
        return 0.0
    if units <= 0 or device_dpi <= 0:
        return 0.0
    return units / device_dpi


def bound_dpi_by_pixels(
    dpi: int,
    width_inches: float,
    height_inches: float,
    max_megapixels: float = DEFAULT_MAX_RENDER_MEGAPIXELS,
) -> int:
    """Lower ``dpi`` until a ``width x height`` inch page fits ``max_megapixels``.

    Pure arithmetic so it can be unit-tested anywhere. Unknown or non-positive
    page dimensions leave ``dpi`` untouched; the result never drops below
    MIN_RENDER_DPI.
    """
    try:
        dpi = int(dpi)
        width_inches = float(width_inches)
        height_inches = float(height_inches)
        max_megapixels = float(max_megapixels)
    except (TypeError, ValueError):
        return dpi
    if dpi <= 0 or width_inches <= 0 or height_inches <= 0 or max_megapixels <= 0:
        return dpi

    budget = max_megapixels * 1_000_000
    pixels = (width_inches * dpi) * (height_inches * dpi)
    if pixels <= budget:
        return dpi

    # pixels scale with dpi^2, so the largest fitting dpi is sqrt(budget/area)
    area = width_inches * height_inches
    fitted = int((budget / area) ** 0.5)
    fitted = max(MIN_RENDER_DPI, min(dpi, fitted))
    logger.warning(
        f"[bound_dpi_by_pixels] {width_inches:.1f}x{height_inches:.1f} in page at "
        f"{dpi} dpi would be {pixels / 1e6:.0f} MP; rendering at {fitted} dpi instead"
    )
    return fitted


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


# Hard ceiling on client-side copies, independent of anything the caller
# translated upstream (models.model.WindowsPrintOptions.MAX_COPIES). A
# dmCopies value can also arrive here via an explicit DEVMODE field that
# never went through that translation, so this function clamps on its own
# rather than trusting the caller - without it, a bogus copies value turns
# into a Python list with page_count * copies entries, which is an easy
# memory-exhaustion/hang bug reachable from a single bad --options value.
MAX_CLIENT_COPIES = 999


def page_order(page_count: int, copies: int, collate: bool):
    """Page indices to draw for client-side copies.

    Collated copies repeat the whole document (1,2,3,1,2,3); uncollated
    copies repeat each page in place (1,1,2,2,3,3).

    Args:
        page_count: Number of pages in the document.
        copies: Number of copies to produce client-side (>= 1). Clamped to
            MAX_CLIENT_COPIES to bound the size of the returned list.
        collate: True for collated output.

    Returns:
        list[int]: zero-based page indices in draw order.
    """
    copies = max(1, int(copies or 1))
    if copies > MAX_CLIENT_COPIES:
        logger.warning(
            f"[page_order] copies={copies} exceeds MAX_CLIENT_COPIES="
            f"{MAX_CLIENT_COPIES}; clamping"
        )
        copies = MAX_CLIENT_COPIES
    if copies == 1:
        return list(range(page_count))
    if collate:
        return [i for _ in range(copies) for i in range(page_count)]
    return [i for i in range(page_count) for _ in range(copies)]
