"""Estimate the post-encode size of a file (pure)."""

from __future__ import annotations


def estimate_output_bytes(
    *,
    size_src_bytes: int,
    bpp_target: float,
    width: int,
    height: int,
    fps: float,
    duration_s: float,
    floor_ratio: float,
) -> int:
    """Estimate output size as max(target-from-bpp, floor fraction of source).

    The floor guards against over-estimating savings on a source that is
    already efficient: we never assume we can shrink below ``floor_ratio`` of
    the original, even if the bpp target would suggest a tiny file.
    """
    size_target = bpp_target * width * height * fps * duration_s / 8.0
    size_floor = size_src_bytes * floor_ratio
    return int(max(size_target, size_floor))
