"""Reference bits-per-pixel targets by resolution band (pure)."""

from __future__ import annotations

from ..core.enums import Codec

# CRF at which the bpp target table represents a "good" (baseline) encode, per codec.
BASELINE_CRF: dict[Codec, int] = {Codec.X265: 22, Codec.SVTAV1: 30}

# Size at equal perceptual quality, relative to x265 (the bpp table is x265-calibrated).
# SVT-AV1 yields ~25-30% smaller files than HEVC at equal quality.
CODEC_EFFICIENCY: dict[Codec, float] = {Codec.X265: 1.0, Codec.SVTAV1: 0.72}


def crf_factor(codec: Codec, crf: int) -> float:
    """Scale the bpp target by the chosen CRF (~ each 6 CRF halves the bitrate).

    Returns >1 for a lower CRF (higher quality, bigger file) and <1 for a higher
    CRF (more compressed), relative to the codec's baseline CRF.
    """
    baseline = BASELINE_CRF.get(codec, 22)
    return 2.0 ** (-(crf - baseline) / 6.0)


def expected_bpp_scale(codec: Codec, crf: int) -> float:
    """Combined CRF and codec-efficiency multiplier applied to the bpp target."""
    return crf_factor(codec, crf) * CODEC_EFFICIENCY.get(codec, 1.0)


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
