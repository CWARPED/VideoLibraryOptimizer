"""Reference bits-per-pixel targets by resolution band (pure)."""

from __future__ import annotations

# Default bands used when the DB has not been consulted: (height_min, height_max, bpp).
DEFAULT_BANDS: list[tuple[int, int, float]] = [
    (0, 576, 0.060),
    (577, 800, 0.050),
    (801, 1100, 0.045),
    (1101, 1600, 0.040),
    (1601, 2200, 0.035),
    (2201, 100000, 0.030),
]

# Multiplier applied to the target for HDR content (keep more detail).
HDR_MULTIPLIER = 1.15


def bpp_target_for_height(
    height: int, bands: list[tuple[int, int, float]] | None = None
) -> float:
    """Return the target bpp for a given source height."""
    table = bands if bands is not None else DEFAULT_BANDS
    for hmin, hmax, bpp in table:
        if hmin <= height <= hmax:
            return bpp
    # Above all bands -> use the last (smallest) target.
    return table[-1][2] if table else 0.030
