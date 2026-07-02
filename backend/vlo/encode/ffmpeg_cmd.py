"""Build ffmpeg argument lists for re-encoding (pure, no execution).

Kept side-effect free and free of shell strings: callers pass the list to
``create_subprocess_exec`` so there is no quoting/injection risk on Windows
or with UNC paths. Tested by snapshotting the argument list.
"""

from __future__ import annotations

from ..core.enums import Codec
from ..core.models import ProbeResult

# 10-bit is forced even for 8-bit sources (better efficiency, less banding).
PIX_FMT_10BIT = "yuv420p10le"
PIX_FMT_8BIT = "yuv420p"  # optional for AV1: lighter/more compatible to decode

DEFAULT_X265_PARAMS = "profile=main10:aq-mode=3:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=60:bframes=6"
DEFAULT_SVTAV1_PARAMS = "tune=0:scd=1:enable-overlays=1"


def gop_for_fps(fps: float, seconds: float = 10.0) -> int:
    """Keyframe interval ~``seconds`` long; defaults to 240 if fps is unknown."""
    if fps and fps > 0:
        return max(1, round(fps * seconds))
    return 240


def subtitle_codec_overrides(probe: ProbeResult) -> list[str]:
    """Per-stream subtitle codec args.

    Everything is copied by default (``-c:s copy``); MP4 ``mov_text`` cannot
    live in MKV cleanly, so it is transcoded to SRT. One track in -> one track
    out, preserving the subtitle count for validation.
    """
    args: list[str] = []
    for out_index, sub in enumerate(probe.subs):
        if sub.codec == "mov_text":
            args += [f"-c:s:{out_index}", "srt"]
    return args


# Lossless audio codecs worth transcoding (big tracks, transparent to re-encode).
_LOSSLESS_AUDIO = {"truehd", "mlp", "flac", "alac"}


def _is_lossless_audio(track) -> bool:
    """True if an audio track is losslessly encoded (safe to shrink to Opus)."""
    codec = (track.codec or "").lower()
    if codec in _LOSSLESS_AUDIO or codec.startswith("pcm_"):
        return True
    # DTS is lossy by default; only DTS-HD Master Audio (profile "…MA…") is lossless.
    if codec in {"dts", "dca"}:
        return "MA" in (track.profile or "").upper()
    return False


def _opus_bitrate_k(channels: int | None) -> int:
    """A transparent Opus bitrate for the given channel count."""
    ch = channels or 2
    if ch <= 1:
        return 96
    if ch == 2:
        return 160
    if ch <= 6:
        return 320
    return 448


def audio_codec_args(probe: ProbeResult, *, transcode_lossless: bool) -> list[str]:
    """Per-stream audio codec args.

    Default: stream-copy every track (bit-perfect). When ``transcode_lossless`` is
    set, lossless tracks (TrueHD/DTS-HD MA/PCM/FLAC…) are re-encoded to Opus at a
    transparent bitrate while already-lossy tracks (AC3/AAC/DTS/E-AC3) are copied
    untouched — so nothing already-compressed is degraded.
    """
    if not transcode_lossless or not probe.audio:
        return ["-c:a", "copy"]
    args: list[str] = []
    for i, tr in enumerate(probe.audio):
        if _is_lossless_audio(tr):
            args += [f"-c:a:{i}", "libopus", f"-b:a:{i}", f"{_opus_bitrate_k(tr.channels)}k"]
            if (tr.channels or 2) > 2:
                args += [f"-mapping_family:a:{i}", "1"]  # required for surround in libopus
        else:
            args += [f"-c:a:{i}", "copy"]
    return args


def color_args(probe: ProbeResult) -> list[str]:
    """Carry source colour metadata over to the output (matters for HDR10)."""
    args: list[str] = []
    if probe.color_primaries:
        args += ["-color_primaries", probe.color_primaries]
    if probe.color_transfer:
        args += ["-color_trc", probe.color_transfer]
    if probe.color_space:
        args += ["-colorspace", probe.color_space]
    return args


def build_encode_command(
    *,
    ffmpeg_bin: str,
    input_path: str,
    output_path: str,
    codec: Codec,
    crf: int,
    preset: str,
    probe: ProbeResult,
    x265_params: str = DEFAULT_X265_PARAMS,
    svtav1_params: str = DEFAULT_SVTAV1_PARAMS,
    title: str | None = None,
    transcode_lossless_audio: bool = False,
    av1_8bit: bool = False,
    progress_to_stdout: bool = True,
) -> list[str]:
    """Return the full ffmpeg argument list for one re-encode.

    Resolution is preserved. All audio/subtitles/attachments/chapters are kept.
    Audio is stream-copied by default; when ``transcode_lossless_audio`` is set,
    lossless tracks are re-encoded to Opus (lossy tracks stay copied). Output is
    always MKV.

    The re-encoded video stream's stale source statistics tags (BPS, DURATION,
    NUMBER_OF_BYTES…) are dropped so the metadata matches the new encode; the
    video language is re-applied. ``title`` overrides the global title tag when
    given ("" clears it).
    """
    args: list[str] = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i", input_path,
        # Explicit mapping: real video only (0:V drops cover art), all audio,
        # all subs, all attachments. Data streams are intentionally not mapped.
        "-map", "0:V?",
        "-map", "0:a?",
        "-map", "0:s?",
        "-map", "0:t?",
        "-map_metadata", "0",
        # Drop the source video stream's (now-stale) statistics tags; the audio
        # and subtitle streams are copied so their stats stay valid.
        "-map_metadata:s:v:0", "-1",
        "-map_chapters", "0",
        "-max_muxing_queue_size", "9999",
    ]
    if probe.video_language:
        args += ["-metadata:s:v:0", f"language={probe.video_language}"]
    if title is not None:
        args += ["-metadata", f"title={title}"]

    if codec is Codec.X265:
        args += [
            "-c:v", "libx265",
            "-pix_fmt", PIX_FMT_10BIT,
            "-preset", preset,
            "-crf", str(crf),
            "-x265-params", x265_params or DEFAULT_X265_PARAMS,
        ]
    elif codec is Codec.SVTAV1:
        args += [
            "-c:v", "libsvtav1",
            "-pix_fmt", PIX_FMT_8BIT if av1_8bit else PIX_FMT_10BIT,
            "-preset", str(preset),
            "-crf", str(crf),
            "-g", str(gop_for_fps(probe.fps)),
            "-svtav1-params", svtav1_params or DEFAULT_SVTAV1_PARAMS,
        ]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unsupported codec: {codec}")

    args += color_args(probe)
    args += audio_codec_args(probe, transcode_lossless=transcode_lossless_audio)
    args += ["-c:s", "copy", "-c:t", "copy"]
    args += subtitle_codec_overrides(probe)

    if progress_to_stdout:
        args += ["-progress", "pipe:1", "-nostats"]

    args.append(output_path)
    return args
