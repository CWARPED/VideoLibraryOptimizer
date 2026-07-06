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
PIX_FMT_8BIT = "yuv420p"  # optional per-job: lighter/more compatible to decode

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
    eight_bit: bool = False,
    progress_to_stdout: bool = True,
) -> list[str]:
    """Return the ffmpeg argument list for phase 1: re-encode the video.

    Only video + audio (+ chapters/metadata) are muxed here — **subtitles are
    deliberately excluded**. Muxing sparse bitmap subtitles (PGS) together with
    a slow video encoder in one pass makes ffmpeg truncate the video stream mid
    file. Subtitles are added afterwards by :func:`build_subtitle_mux_command`,
    a fast stream-copy remux where all streams are already complete. Audio is
    always stream-copied; output is MKV.

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
        # Real video only (0:V drops cover art) + all audio. Subtitles and
        # attachments are added in the phase-2 subtitle remux, not here.
        "-map", "0:V?",
        "-map", "0:a?",
        "-map_metadata", "0",
        # Drop the source video stream's (now-stale) statistics tags.
        "-map_metadata:s:v:0", "-1",
        "-map_chapters", "0",
        "-max_muxing_queue_size", "9999",
    ]
    if probe.video_language:
        args += ["-metadata:s:v:0", f"language={probe.video_language}"]
    if title is not None:
        args += ["-metadata", f"title={title}"]

    if codec is Codec.X265:
        # 8-bit needs the Main profile (Main10 forces 10-bit input).
        x265p = x265_params or DEFAULT_X265_PARAMS
        if eight_bit:
            x265p = x265p.replace("main10", "main")
        args += [
            "-c:v", "libx265",
            "-pix_fmt", PIX_FMT_8BIT if eight_bit else PIX_FMT_10BIT,
            "-preset", preset,
            "-crf", str(crf),
            "-x265-params", x265p,
        ]
    elif codec is Codec.SVTAV1:
        args += [
            "-c:v", "libsvtav1",
            "-pix_fmt", PIX_FMT_8BIT if eight_bit else PIX_FMT_10BIT,
            "-preset", str(preset),
            "-crf", str(crf),
            "-g", str(gop_for_fps(probe.fps)),
            "-svtav1-params", svtav1_params or DEFAULT_SVTAV1_PARAMS,
        ]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unsupported codec: {codec}")

    args += color_args(probe)
    args += ["-c:a", "copy"]

    if progress_to_stdout:
        args += ["-progress", "pipe:1", "-nostats"]

    args.append(output_path)
    return args


def build_subtitle_mux_command(
    *,
    ffmpeg_bin: str,
    video_audio_path: str,
    source_path: str,
    output_path: str,
    probe: ProbeResult,
) -> list[str]:
    """Phase 2: copy the encoded video+audio and add subtitles/attachments.

    Input 0 is the phase-1 output (encoded video + copied audio); input 1 is the
    original source, used only for its subtitle and attachment streams. Nothing
    is re-encoded (except MP4 ``mov_text`` -> SRT), so all streams are complete
    and ``-max_interleave_delta 0`` gives a correctly interleaved, seekable MKV
    — the sparse-subtitle interleave/truncation problem cannot occur here.
    """
    args = [
        ffmpeg_bin, "-hide_banner", "-y",
        "-i", video_audio_path,
        "-i", source_path,
        "-map", "0:v", "-map", "0:a?",
        "-map", "1:s?", "-map", "1:t?",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-c", "copy",
        "-max_interleave_delta", "0",
        "-max_muxing_queue_size", "9999",
    ]
    args += subtitle_codec_overrides(probe)
    args.append(output_path)
    return args
