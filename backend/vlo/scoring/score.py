"""Composite re-encode priority score (pure, no I/O)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.models import Classification, ProbeResult, ScoreResult
from .estimate import estimate_output_bytes
from .reference import HDR_MULTIPLIER, bpp_target_for_height

# Overhead saturates at 8x the target bitrate (log2(8) == 3).
_OVERHEAD_LOG_CAP = 3.0


@dataclass(slots=True)
class ScoringConfig:
    bands: list[tuple[int, int, float]] | None = None
    animation_bands: list[tuple[int, int, float]] | None = None
    weight_overhead: float = 0.5
    weight_gain: float = 0.5
    gain_ref_gb: float = 10.0
    min_overhead_ratio: float = 1.1
    exclude_dolby_vision: bool = True
    # Representative floor used for ranking (actual per-job floor is chosen at encode time).
    rank_floor_ratio: float = 0.55

    def bands_for(self, content_type: str) -> list[tuple[int, int, float]] | None:
        """Pick the bpp target table for the content type (falls back to live-action)."""
        if content_type == "animation" and self.animation_bands:
            return self.animation_bands
        return self.bands


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_score(
    probe: ProbeResult, classification: Classification, config: ScoringConfig
) -> ScoreResult:
    """Compute the composite score and candidate eligibility for one file."""
    pixels_per_sec = probe.width * probe.height * probe.fps

    # Guard against unusable probe data.
    if pixels_per_sec <= 0 or probe.duration_s <= 0 or probe.video_bitrate_bps <= 0:
        return ScoreResult(
            bpp_real=0.0, bpp_target=0.0, overhead_ratio=0.0,
            est_out_bytes=probe.size_bytes, est_gain_bytes=0, score=0.0,
            excluded_reason="unknown bitrate/resolution",
        )

    bands = config.bands_for(classification.content_type)
    bpp_target = bpp_target_for_height(probe.height, bands)
    if probe.is_hdr:
        bpp_target *= HDR_MULTIPLIER

    bpp_real = probe.video_bitrate_bps / pixels_per_sec
    overhead_ratio = bpp_real / bpp_target if bpp_target > 0 else 0.0

    est_out = estimate_output_bytes(
        size_src_bytes=probe.size_bytes,
        bpp_target=bpp_target,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration_s=probe.duration_s,
        floor_ratio=config.rank_floor_ratio,
    )
    est_gain = max(0, probe.size_bytes - est_out)

    # Exclusions (still computed so the UI can show why).
    excluded: str | None = None
    if config.exclude_dolby_vision and probe.is_dolby_vision:
        excluded = "Dolby Vision (excluded by default)"
    elif overhead_ratio < config.min_overhead_ratio:
        excluded = "already efficient"
    elif est_gain <= 0:
        excluded = "no estimated gain"

    overhead_comp = _clamp(math.log2(overhead_ratio), 0.0, _OVERHEAD_LOG_CAP) / _OVERHEAD_LOG_CAP \
        if overhead_ratio > 0 else 0.0
    gain_gb = est_gain / 1e9
    gain_comp = _clamp(gain_gb / config.gain_ref_gb, 0.0, 1.0) if config.gain_ref_gb > 0 else 0.0

    score = 100.0 * (config.weight_overhead * overhead_comp + config.weight_gain * gain_comp)

    return ScoreResult(
        bpp_real=bpp_real,
        bpp_target=bpp_target,
        overhead_ratio=overhead_ratio,
        est_out_bytes=est_out,
        est_gain_bytes=est_gain,
        score=round(score, 2),
        excluded_reason=excluded,
    )
