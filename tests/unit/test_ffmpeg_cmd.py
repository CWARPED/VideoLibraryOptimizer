"""Tests for ffmpeg argument construction (pure, no ffmpeg execution)."""

from __future__ import annotations

from vlo.core.enums import Codec
from vlo.core.models import AudioTrack, ProbeResult, SubTrack
from vlo.encode.ffmpeg_cmd import (
    PIX_FMT_8BIT,
    PIX_FMT_10BIT,
    build_encode_command,
    gop_for_fps,
    subtitle_codec_overrides,
)


def make_probe(subs=None, **kw) -> ProbeResult:
    base = dict(
        path="in.mkv", size_bytes=1, duration_s=60.0, width=1920, height=1080,
        fps=24.0, vcodec="h264", pix_fmt="yuv420p", is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=10_000_000, overall_bitrate_bps=10_000_000,
    )
    base.update(kw)
    p = ProbeResult(**base)
    p.subs = subs or []
    return p


def _adjacent(args, flag):
    """Return the value following a flag in an arg list (or None)."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def test_gop_for_fps():
    assert gop_for_fps(24.0) == 240
    assert gop_for_fps(23.976) == 240
    assert gop_for_fps(0) == 240


def test_x265_command_core_flags():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=make_probe(),
    )
    assert _adjacent(args, "-c:v") == "libx265"
    assert _adjacent(args, "-pix_fmt") == PIX_FMT_10BIT
    assert _adjacent(args, "-crf") == "20"
    assert _adjacent(args, "-preset") == "slow"
    assert "-c:a" in args and _adjacent(args, "-c:a") == "copy"
    assert _adjacent(args, "-c:s") == "copy"
    assert _adjacent(args, "-c:t") == "copy"
    # Explicit mapping that drops cover art and data streams.
    assert "0:V?" in args and "0:a?" in args and "0:s?" in args and "0:t?" in args
    assert args[-1] == "out.mkv"
    assert "-progress" in args


def test_svtav1_command_core_flags():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.SVTAV1, crf=28, preset="6", probe=make_probe(fps=25.0),
    )
    assert _adjacent(args, "-c:v") == "libsvtav1"
    assert _adjacent(args, "-pix_fmt") == PIX_FMT_10BIT  # 10-bit by default
    assert _adjacent(args, "-preset") == "6"
    assert _adjacent(args, "-g") == "250"  # 25fps * 10s
    assert "-svtav1-params" in args


def test_eight_bit_option_av1():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.SVTAV1, crf=28, preset="6", probe=make_probe(), eight_bit=True,
    )
    assert _adjacent(args, "-pix_fmt") == PIX_FMT_8BIT


def test_eight_bit_option_x265_switches_profile_to_main():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=make_probe(), eight_bit=True,
    )
    assert _adjacent(args, "-pix_fmt") == PIX_FMT_8BIT
    params = _adjacent(args, "-x265-params")
    assert "profile=main" in params and "main10" not in params
    # 10-bit default still uses Main10.
    ten = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=make_probe(),
    )
    assert _adjacent(ten, "-pix_fmt") == PIX_FMT_10BIT
    assert "main10" in _adjacent(ten, "-x265-params")


def test_mov_text_subtitle_transcoded_to_srt():
    subs = [
        SubTrack(index=2, codec="hdmv_pgs_subtitle"),
        SubTrack(index=3, codec="mov_text"),
        SubTrack(index=4, codec="subrip"),
    ]
    overrides = subtitle_codec_overrides(make_probe(subs=subs))
    # Only the mov_text stream (output index 1) is overridden to srt.
    assert overrides == ["-c:s:1", "srt"]


def test_video_stream_stats_dropped_and_language_reapplied():
    probe = make_probe()
    probe.video_language = "fre"
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=probe,
    )
    # Stale source video stats (BPS/DURATION/...) are dropped for the video stream.
    assert _adjacent(args, "-map_metadata:s:v:0") == "-1"
    # Video language is re-applied (cleared by the line above otherwise).
    assert _adjacent(args, "-metadata:s:v:0") == "language=fre"


def test_title_override_applied():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=make_probe(), title="New Title",
    )
    assert _adjacent(args, "-metadata") == "title=New Title"


def test_no_title_override_by_default():
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=make_probe(),
    )
    # Without an explicit title, we don't add a global -metadata title= override.
    assert "title=" not in " ".join(args)


def _with_audio(probe, tracks):
    probe.audio = tracks
    return probe


def test_audio_stream_copied_by_default():
    probe = _with_audio(make_probe(), [AudioTrack(index=0, codec="truehd", channels=8)])
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=probe,
    )
    assert _adjacent(args, "-c:a") == "copy"
    assert "libopus" not in args


def test_lossless_audio_transcoded_to_opus_lossy_copied():
    probe = _with_audio(make_probe(), [
        AudioTrack(index=0, codec="truehd", channels=8),   # lossless 7.1 -> opus 448k
        AudioTrack(index=1, codec="ac3", channels=6),      # already lossy -> copy
        AudioTrack(index=2, codec="flac", channels=2),     # lossless stereo -> opus 160k
    ])
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=probe,
        transcode_lossless_audio=True,
    )
    assert "-c:a" not in args  # no blanket copy; per-stream instead
    assert _adjacent(args, "-c:a:0") == "libopus"
    assert _adjacent(args, "-b:a:0") == "448k"
    assert _adjacent(args, "-mapping_family:a:0") == "1"  # surround needs it
    assert _adjacent(args, "-c:a:1") == "copy"            # ac3 untouched
    assert _adjacent(args, "-c:a:2") == "libopus"
    assert _adjacent(args, "-b:a:2") == "160k"
    assert "-mapping_family:a:2" not in args               # stereo: no surround mapping


def test_dts_hd_ma_is_lossless_but_plain_dts_is_copied():
    probe = _with_audio(make_probe(), [
        AudioTrack(index=0, codec="dts", channels=8, profile="DTS-HD MA"),
        AudioTrack(index=1, codec="dts", channels=6, profile="DTS"),
    ])
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=20, preset="slow", probe=probe,
        transcode_lossless_audio=True,
    )
    assert _adjacent(args, "-c:a:0") == "libopus"  # DTS-HD Master Audio (lossless)
    assert _adjacent(args, "-c:a:1") == "copy"     # plain DTS (lossy) kept as-is


def test_color_metadata_preserved_for_hdr():
    probe = make_probe(
        is_hdr=True, color_primaries="bt2020", color_transfer="smpte2084",
        color_space="bt2020nc",
    )
    args = build_encode_command(
        ffmpeg_bin="ffmpeg", input_path="in.mkv", output_path="out.mkv",
        codec=Codec.X265, crf=18, preset="slow", probe=probe,
    )
    assert _adjacent(args, "-color_primaries") == "bt2020"
    assert _adjacent(args, "-color_trc") == "smpte2084"
    assert _adjacent(args, "-colorspace") == "bt2020nc"
