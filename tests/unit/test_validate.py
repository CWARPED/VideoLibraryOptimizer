"""Tests for post-encode validation (pure)."""

from __future__ import annotations

from vlo.core.enums import Codec
from vlo.core.models import AudioTrack, ProbeResult, SubTrack


def make_probe(*, duration=3600.0, size=4_000_000_000, n_audio=2, n_subs=1,
               vcodec="hevc", pix_fmt="yuv420p10le") -> ProbeResult:
    p = ProbeResult(
        path="x", size_bytes=size, duration_s=duration, width=1920, height=1080,
        fps=24.0, vcodec=vcodec, pix_fmt=pix_fmt, is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=5_000_000, overall_bitrate_bps=5_000_000,
    )
    p.audio = [AudioTrack(index=i, codec="ac3") for i in range(n_audio)]
    p.subs = [SubTrack(index=i, codec="subrip") for i in range(n_subs)]
    return p


from vlo.encode.validate import validate_output  # noqa: E402


def test_valid_output_passes():
    src = make_probe(size=8_000_000_000, vcodec="h264", pix_fmt="yuv420p")
    out = make_probe(size=4_000_000_000)  # half the size, hevc 10-bit, same tracks
    report = validate_output(src, out, codec=Codec.X265)
    assert report.ok
    assert report.gain_bytes == 4_000_000_000


def test_missing_audio_track_fails():
    src = make_probe(n_audio=3, vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(n_audio=2, size=4_000_000_000)
    report = validate_output(src, out, codec=Codec.X265)
    assert not report.ok
    assert any(c.name == "audio_tracks" and not c.passed for c in report.checks)


def test_missing_subtitle_fails():
    src = make_probe(n_subs=2, vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(n_subs=1, size=4_000_000_000)
    report = validate_output(src, out, codec=Codec.X265)
    assert not report.ok
    assert any(c.name == "subtitle_tracks" and not c.passed for c in report.checks)


def test_no_gain_fails():
    src = make_probe(size=4_000_000_000, vcodec="h264", pix_fmt="yuv420p")
    out = make_probe(size=4_500_000_000)  # grew!
    report = validate_output(src, out, codec=Codec.X265)
    assert not report.ok
    assert any(c.name == "size_gain" and not c.passed for c in report.checks)


def test_duration_mismatch_fails():
    src = make_probe(duration=3600.0, vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(duration=3000.0, size=4_000_000_000)  # 10 min short
    report = validate_output(src, out, codec=Codec.X265)
    assert not report.ok
    assert any(c.name == "duration" and not c.passed for c in report.checks)


def test_vfr_relaxes_duration_tolerance():
    src = make_probe(duration=3600.0, vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(duration=3610.0, size=4_000_000_000)  # 0.28% off
    strict = validate_output(src, out, codec=Codec.X265, duration_tolerance_pct=0.1)
    relaxed = validate_output(src, out, codec=Codec.X265, duration_tolerance_pct=0.1, is_vfr=True)
    assert not any(c.name == "duration" and c.passed for c in strict.checks)
    assert any(c.name == "duration" and c.passed for c in relaxed.checks)


def test_wrong_codec_fails():
    src = make_probe(vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(vcodec="av1", size=4_000_000_000)
    report = validate_output(src, out, codec=Codec.X265)  # expected hevc, got av1
    assert not report.ok
    assert any(c.name == "video_codec" and not c.passed for c in report.checks)


def test_8bit_output_fails_pixel_check():
    src = make_probe(vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(pix_fmt="yuv420p", size=4_000_000_000)  # not 10-bit
    report = validate_output(src, out, codec=Codec.X265)
    assert any(c.name == "pixel_format" and not c.passed for c in report.checks)


def test_8bit_output_passes_when_8bit_requested():
    src = make_probe(vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(pix_fmt="yuv420p", size=4_000_000_000)  # 8-bit, as requested
    report = validate_output(src, out, codec=Codec.X265, eight_bit=True)
    assert report.ok
    assert any(c.name == "pixel_format" and c.passed for c in report.checks)
    # A 10-bit output when 8-bit was requested should now fail the check.
    out10 = make_probe(pix_fmt="yuv420p10le", size=4_000_000_000)
    r2 = validate_output(src, out10, codec=Codec.X265, eight_bit=True)
    assert any(c.name == "pixel_format" and not c.passed for c in r2.checks)


def test_truncated_audio_fails():
    """Regression: audio that stops mid-file (e.g. Opus transcode truncated) while
    the video runs to the end must fail validation, not be silently confirmed."""
    src = make_probe(duration=6000.0, vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(duration=6000.0, size=4_000_000_000)
    # Audio ends at 1700s of a 6000s file -> truncated.
    bad = validate_output(src, out, codec=Codec.X265, audio_end_s=1700.0)
    assert not bad.ok
    assert any(c.name == "audio_complete" and not c.passed for c in bad.checks)
    # Audio reaching (within tolerance of) the end passes.
    good = validate_output(src, out, codec=Codec.X265, audio_end_s=5990.0)
    assert any(c.name == "audio_complete" and c.passed for c in good.checks)
    # Not measured -> no check added (no regression for callers that don't pass it).
    none = validate_output(src, out, codec=Codec.X265)
    assert not any(c.name == "audio_complete" for c in none.checks)


def test_decode_failure_fails():
    src = make_probe(vcodec="h264", pix_fmt="yuv420p", size=8_000_000_000)
    out = make_probe(size=4_000_000_000)
    report = validate_output(src, out, codec=Codec.X265, decoded_ok=False)
    assert not report.ok
    assert any(c.name == "readable" and not c.passed for c in report.checks)
