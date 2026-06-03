"""Tests for the scoring engine (pure)."""

from __future__ import annotations

from vlo.core.enums import MediaKind
from vlo.core.models import Classification, ProbeResult
from vlo.scoring.estimate import estimate_output_bytes
from vlo.scoring.reference import bpp_target_for_height
from vlo.scoring.score import ScoringConfig, compute_score

MOVIE = Classification(kind=MediaKind.MOVIE, title="X")
CFG = ScoringConfig()


def make_probe(
    *,
    width=1920,
    height=1080,
    fps=24.0,
    duration_s=3600.0,
    video_bitrate_bps=15_000_000,
    size_bytes=8_000_000_000,
    is_hdr=False,
    is_dv=False,
) -> ProbeResult:
    return ProbeResult(
        path="x.mkv", size_bytes=size_bytes, duration_s=duration_s,
        width=width, height=height, fps=fps, vcodec="h264", pix_fmt="yuv420p",
        is_hdr=is_hdr, is_dolby_vision=is_dv,
        video_bitrate_bps=video_bitrate_bps, overall_bitrate_bps=video_bitrate_bps,
    )


def test_bpp_target_bands():
    assert bpp_target_for_height(480) == 0.060
    assert bpp_target_for_height(720) == 0.050
    assert bpp_target_for_height(1080) == 0.045
    assert bpp_target_for_height(2160) == 0.035
    assert bpp_target_for_height(4320) == 0.030  # above bands -> smallest target


def test_bloated_file_is_candidate():
    # 1080p @ 24fps, 15 Mbps -> bpp_real ~0.30, target 0.045 -> overhead ~6.7x
    r = compute_score(make_probe(), MOVIE, CFG)
    assert r.is_candidate
    assert r.overhead_ratio > 5
    assert r.est_gain_bytes > 0
    assert r.score > 0


def test_efficient_file_excluded():
    # Low bitrate -> overhead below threshold.
    r = compute_score(make_probe(video_bitrate_bps=2_000_000), MOVIE, CFG)
    assert not r.is_candidate
    assert r.excluded_reason == "already efficient"


def test_dolby_vision_excluded_by_default():
    r = compute_score(make_probe(is_dv=True), MOVIE, CFG)
    assert not r.is_candidate
    assert "Dolby Vision" in r.excluded_reason


def test_dolby_vision_allowed_when_configured():
    cfg = ScoringConfig(exclude_dolby_vision=False)
    r = compute_score(make_probe(is_dv=True), MOVIE, cfg)
    assert r.is_candidate


def test_hdr_raises_target_lowering_overhead():
    base = compute_score(make_probe(), MOVIE, CFG)
    hdr = compute_score(make_probe(is_hdr=True), MOVIE, CFG)
    assert hdr.bpp_target > base.bpp_target
    assert hdr.overhead_ratio < base.overhead_ratio


def test_unknown_bitrate_excluded():
    r = compute_score(make_probe(video_bitrate_bps=0), MOVIE, CFG)
    assert not r.is_candidate
    assert r.excluded_reason == "unknown bitrate/resolution"


def test_score_increases_with_overhead():
    low = compute_score(make_probe(video_bitrate_bps=8_000_000), MOVIE, CFG)
    high = compute_score(make_probe(video_bitrate_bps=25_000_000), MOVIE, CFG)
    assert high.score > low.score


def test_estimate_floor_dominates_for_efficient_source():
    # Tiny target but floor keeps it at floor_ratio * size.
    out = estimate_output_bytes(
        size_src_bytes=1_000_000_000, bpp_target=0.001,
        width=1920, height=1080, fps=24, duration_s=60, floor_ratio=0.55,
    )
    assert out == int(1_000_000_000 * 0.55)


def test_animation_uses_lower_target():
    from vlo.scoring.reference import DEFAULT_BANDS
    anim_bands = [(0, 576, 0.035), (577, 800, 0.028), (801, 1100, 0.025),
                  (1101, 1600, 0.022), (1601, 2200, 0.020), (2201, 100000, 0.017)]
    cfg = ScoringConfig(bands=DEFAULT_BANDS, animation_bands=anim_bands)
    probe = make_probe(video_bitrate_bps=6_000_000)  # 1080p, modest bitrate

    live = compute_score(probe, Classification(kind=MediaKind.MOVIE, content_type="live_action"), cfg)
    anim = compute_score(probe, Classification(kind=MediaKind.MOVIE, content_type="animation"), cfg)

    # Lower animation target -> higher overhead for the same bitrate.
    assert anim.bpp_target < live.bpp_target
    assert anim.overhead_ratio > live.overhead_ratio


def test_animation_falls_back_to_live_bands_when_absent():
    from vlo.scoring.reference import DEFAULT_BANDS
    cfg = ScoringConfig(bands=DEFAULT_BANDS, animation_bands=None)
    probe = make_probe()
    anim = compute_score(probe, Classification(kind=MediaKind.MOVIE, content_type="animation"), cfg)
    live = compute_score(probe, Classification(kind=MediaKind.MOVIE, content_type="live_action"), cfg)
    assert anim.bpp_target == live.bpp_target


def test_estimate_target_dominates_when_above_floor():
    # target = 0.045*1920*1080*24*3600/8 ~= 1.0 GB; floor = 0.55 GB for a 1 GB source.
    size_src = 1_000_000_000
    out = estimate_output_bytes(
        size_src_bytes=size_src, bpp_target=0.045,
        width=1920, height=1080, fps=24, duration_s=3600, floor_ratio=0.55,
    )
    expected_target = int(0.045 * 1920 * 1080 * 24 * 3600 / 8)
    assert out == expected_target
    assert out > size_src * 0.55  # target above the floor
