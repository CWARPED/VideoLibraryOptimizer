"""Full end-to-end job pipeline with real ffmpeg, through the JobManager."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from vlo.config import Settings
from vlo.core.enums import Codec, JobState
from vlo.core.models import EncodeProfile
from vlo.deps import build_app_state

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not found")


def _make_bloated_source(path: Path) -> None:
    """A deliberately huge (lossless) source so a CRF re-encode is much smaller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    srt = path.parent / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\nHi\n", encoding="utf-8")
    cmd = [
        FFMPEG, "-y", "-hide_banner",
        "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=24:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=6",
        "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
        "-c:a", "flac", "-c:s", "srt", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_full_pipeline_encode_confirm_replace(tmp_path):
    src = tmp_path / "nas" / "Movies" / "Bloated.Movie.480p.mkv"
    _make_bloated_source(src)
    original_size = src.stat().st_size

    settings = Settings(
        db_path=tmp_path / "vlo.db",
        work_dir=tmp_path / "work",
        disk_space_margin_bytes=0,
    )
    state = build_app_state(settings)
    # Speed up the encode for the test (ultrafast, high CRF -> small + quick).
    state.settings_repo.upsert_profile(EncodeProfile(
        name="Light", crf_x265=32, crf_av1=50, preset_x265="ultrafast", preset_av1=12,
        floor_x265=0.7, floor_av1=0.65,
        x265_params="profile=main10", svtav1_params="",
    ))

    # Scan to populate the library (classification + scoring + cache).
    await state.run_scan(str(tmp_path / "nas"))
    movies = state.scan_repo.list_movies(only_candidates=False)
    assert len(movies) == 1
    mf = movies[0]

    await state.job_manager.start()
    try:
        batch = state.job_manager.enqueue([mf], Codec.X265, "Light")
        job_id = state.jobs_repo.list(batch_id=batch)[0].id

        # Wait for encode + validation.
        job = None
        for _ in range(1500):  # up to ~30s
            job = state.jobs_repo.get(job_id)
            if job.state is JobState.AWAITING_CONFIRMATION:
                break
            if job.state is JobState.FAILED:
                pytest.fail(f"job failed: {job.error_message}")
            await asyncio.sleep(0.02)
        assert job.state is JobState.AWAITING_CONFIRMATION
        assert job.gain_bytes and job.gain_bytes > 0  # real size reduction

        await state.job_manager.confirm(job_id)
        done = state.jobs_repo.get(job_id)
        assert done.state is JobState.DONE
        assert src.exists()
        assert src.stat().st_size < original_size  # original replaced by smaller file
        assert not Path(job.work_dir).exists()  # work dir cleaned

        # Cache now points at the (same .mkv) replaced file with new size.
        assert state.scan_repo.get_by_id(mf.id).size_bytes == src.stat().st_size

        # Refresh re-probes + re-scores the processed file so it no longer looks
        # like a heavy candidate (lower bitrate -> lower overhead, often excluded).
        before = state.scan_repo.get_by_id(mf.id)
        await state.refresh_media_file(mf.id)
        after = state.scan_repo.get_by_id(mf.id)
        assert after.probe.video_bitrate_bps < before.probe.video_bitrate_bps
        assert after.score.overhead_ratio < before.score.overhead_ratio
        # Marked as processed -> no longer proposed as a candidate.
        assert after.reencoded_at is not None
        assert mf.id not in {m.id for m in state.scan_repo.list_movies(only_candidates=True)}
    finally:
        await state.job_manager.stop()
        state.db.close()
