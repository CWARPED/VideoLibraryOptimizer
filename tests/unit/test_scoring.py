"""Tests for the scoring engine (pure)."""

from __future__ import annotations

from vlo.core.enums import Codec, MediaKind
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


def test_estimate_preserves_non_video_bytes():
    # Audio+subs+container (size - video) are copied; only the video shrinks.
    pps = 1920 * 1080 * 24
    out = estimate_output_bytes(
        size_src_bytes=100_000_000, video_bitrate_bps=10_000_000, bpp_target=0.0001,
        pixels_per_sec=pps, duration_s=60, crf_factor=1.0, video_floor_ratio=0.10,
    )
    video_bytes = 10_000_000 * 60 / 8.0           # 75 MB of video
    other_bytes = 100_000_000 - video_bytes       # 25 MB preserved
    # Tiny target -> video clamped to the 10% video floor.
    assert out == int(other_bytes + 0.10 * video_bytes)


def test_estimate_never_grows_above_source_video():
    pps = 1920 * 1080 * 24
    out = estimate_output_bytes(
        size_src_bytes=100_000_000, video_bitrate_bps=5_000_000, bpp_target=10.0,
        pixels_per_sec=pps, duration_s=60, crf_factor=5.0,  # absurdly high target
    )
    # Output never exceeds the source (can't grow by re-encoding down).
    assert out <= 100_000_000


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


def test_estimate_target_drives_video_size():
    # Expected video bpp (target) sits between the floor and the source bpp.
    pps = 1920 * 1080 * 24
    out = estimate_output_bytes(
        size_src_bytes=8_000_000_000, video_bitrate_bps=15_000_000, bpp_target=0.045,
        pixels_per_sec=pps, duration_s=3600, crf_factor=1.0, video_floor_ratio=0.10,
    )
    video_bytes = 15_000_000 * 3600 / 8.0
    other_bytes = 8_000_000_000 - video_bytes
    est_video = 0.045 * pps * 3600 / 8.0
    assert out == int(other_bytes + est_video)


def test_gain_is_monotonic_with_crf():
    # Regression: Archive (lowest CRF, highest quality) must show the LEAST gain,
    # and a more compressed profile (higher CRF) the MOST gain.
    probe = make_probe()  # bloated 1080p source

    def gain(crf):
        cfg = ScoringConfig(rank_codec=Codec.X265, rank_crf=crf)
        return compute_score(probe, MOVIE, cfg).est_gain_bytes

    assert gain(18) < gain(22) < gain(28)  # Archive < Balanced < Mini


def test_av1_estimates_more_gain_than_x265():
    # AV1 is more efficient at equal quality -> smaller output -> larger gain
    # than x265 at each codec's baseline CRF.
    probe = make_probe()
    x265 = compute_score(probe, MOVIE, ScoringConfig(rank_codec=Codec.X265, rank_crf=22))
    av1 = compute_score(probe, MOVIE, ScoringConfig(rank_codec=Codec.SVTAV1, rank_crf=30))
    assert av1.est_gain_bytes > x265.est_gain_bytes
    assert av1.est_out_bytes < x265.est_out_bytes
