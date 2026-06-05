"""Estimate the post-encode size of a file (pure)."""

from __future__ import annotations


def estimate_output_bytes(
    *,
    size_src_bytes: int,
    video_bitrate_bps: float,
    bpp_target: float,
    pixels_per_sec: float,
    duration_s: float,
    crf_factor: float,
    video_floor_ratio: float = 0.10,
) -> int:
    """Estimate the output size after re-encoding only the video stream.

    Audio, subtitles and container overhead are copied untouched, so the gain
    comes solely from shrinking the video stream. The expected output video
    bitrate is the per-resolution/content ``bpp_target`` scaled by ``crf_factor``
    (so the chosen CRF actually drives the estimate). Two clamps keep it sane:
    we never predict a video *larger* than the source, and never smaller than
    ``video_floor_ratio`` of the source video (guard against over-optimism).
    """
    if pixels_per_sec <= 0 or duration_s <= 0 or video_bitrate_bps <= 0:
        return size_src_bytes

    video_bytes = video_bitrate_bps * duration_s / 8.0
    other_bytes = max(0.0, size_src_bytes - video_bytes)  # preserved (audio+subs+container)

    bpp_src = video_bitrate_bps / pixels_per_sec
    expected_bpp = bpp_target * crf_factor
    est_bpp = min(bpp_src, expected_bpp)               # can't grow by re-encoding down
    est_bpp = max(est_bpp, video_floor_ratio * bpp_src)  # video safety floor

    est_video = est_bpp * pixels_per_sec * duration_s / 8.0
    return int(other_bytes + est_video)
