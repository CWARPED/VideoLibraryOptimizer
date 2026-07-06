"""End-to-end encode pipeline test using real ffmpeg/ffprobe.

Generates a tiny clip with two audio tracks and one subtitle, then runs the
actual encode command + validation for both codecs. Marked ``integration``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from vlo.core.enums import Codec
from vlo.encode.ffmpeg_cmd import build_encode_command, build_subtitle_mux_command
from vlo.encode.runner import EncodeRunner
from vlo.encode.validate import validate_output
from vlo.jobs.pipeline import decode_check
from vlo.probe.service import ProbeService

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not found")


def _has_encoder(name: str) -> bool:
    if not FFMPEG:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    return name in out


def _make_source(dir_: Path) -> Path:
    """Create a 10s clip: 1 video + 2 audio + 1 subtitle, in MKV."""
    srt = dir_ / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:05,000\nHello\n", encoding="utf-8")
    src = dir_ / "source.mkv"
    cmd = [
        FFMPEG, "-y", "-hide_banner",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=10",
        "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
        "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
        "-shortest", str(src),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return src


@requires_ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("codec,encoder,crf,preset", [
    (Codec.X265, "libx265", 30, "ultrafast"),
    (Codec.SVTAV1, "libsvtav1", 50, "12"),
])
async def test_encode_preserves_streams_and_is_valid(tmp_path, codec, encoder, crf, preset):
    if not _has_encoder(encoder):
        pytest.skip(f"{encoder} not available in this ffmpeg build")

    src = _make_source(tmp_path)
    probe_svc = ProbeService(FFPROBE)
    src_probe = probe_svc.probe(str(src))
    assert src_probe.n_audio == 2
    assert src_probe.n_subs == 1

    out = tmp_path / "out.mkv"
    video_mkv = tmp_path / "video.mkv"
    # Phase 1: encode video + audio (no subtitles).
    args = build_encode_command(
        ffmpeg_bin=FFMPEG, input_path=str(src), output_path=str(video_mkv),
        codec=codec, crf=crf, preset=preset, probe=src_probe,
    )

    progresses: list[float] = []
    result = await EncodeRunner().run(
        args, duration_s=src_probe.duration_s,
        on_progress=lambda p: progresses.append(p.progress),
    )
    assert not result.cancelled and result.returncode == 0
    assert video_mkv.exists()
    # Progress was reported and reached (near) completion.
    assert progresses and progresses[-1] >= 0.9

    # Phase 2: add the subtitles back from the source via a stream-copy remux.
    mux_args = build_subtitle_mux_command(
        ffmpeg_bin=FFMPEG, video_audio_path=str(video_mkv), source_path=str(src),
        output_path=str(out), probe=src_probe,
    )
    mux_result = await EncodeRunner().run(mux_args, duration_s=src_probe.duration_s)
    assert not mux_result.cancelled and mux_result.returncode == 0
    assert out.exists()

    out_probe = probe_svc.probe(str(out))
    decoded_ok = await decode_check(FFMPEG, out)
    report = validate_output(src_probe, out_probe, codec=codec, decoded_ok=decoded_ok)

    by_name = {c.name: c for c in report.checks}
    # Structural guarantees (size gain is not asserted: a 2s synthetic clip may grow).
    assert by_name["audio_tracks"].passed, by_name["audio_tracks"].detail
    assert by_name["subtitle_tracks"].passed, by_name["subtitle_tracks"].detail
    assert by_name["video_codec"].passed, by_name["video_codec"].detail
    assert by_name["pixel_format"].passed, by_name["pixel_format"].detail
    assert by_name["duration"].passed, by_name["duration"].detail
    assert by_name["readable"].passed, by_name["readable"].detail


@requires_ffmpeg
def test_encode_and_decodecheck_run_on_selector_event_loop(tmp_path):
    """Regression: ffmpeg must run under a SelectorEventLoop.

    uvicorn uses the SelectorEventLoop on Windows in --reload mode, which does
    NOT support asyncio subprocesses (NotImplementedError). The thread-based
    runner/decode_check must work there. This test drives the coroutines on an
    explicit SelectorEventLoop to reproduce that environment.
    """
    src = _make_source(tmp_path)
    probe_svc = ProbeService(FFPROBE)
    src_probe = probe_svc.probe(str(src))
    out = tmp_path / "out.mkv"
    args = build_encode_command(
        ffmpeg_bin=FFMPEG, input_path=str(src), output_path=str(out),
        codec=Codec.X265, crf=30, preset="ultrafast", probe=src_probe,
    )

    progresses: list[float] = []
    loop = asyncio.SelectorEventLoop()
    try:
        result = loop.run_until_complete(
            EncodeRunner().run(
                args, duration_s=src_probe.duration_s,
                on_progress=lambda p: progresses.append(p.progress),
            )
        )
        assert result.returncode == 0
        assert out.exists()
        # decode_check must also work on the selector loop.
        assert loop.run_until_complete(decode_check(FFMPEG, out)) is True
    finally:
        loop.close()
    # Progress callbacks were marshalled to the loop and ran.
    assert progresses and progresses[-1] >= 0.9
