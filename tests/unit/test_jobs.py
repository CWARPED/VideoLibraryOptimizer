"""Tests for the job manager state machine and safe replacement.

Uses fake encode/probe/decode dependencies and real temp files, so no ffmpeg
is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vlo.config import Settings
from vlo.core.enums import Codec, JobState, MediaKind
from vlo.core.models import Classification, MediaFile, ProbeResult, ScoreResult
from vlo.encode.runner import EncodeProgress, EncodeResult
from vlo.jobs.manager import JobManager
from vlo.storage.repo_jobs import JobsRepo
from vlo.storage.repo_scan import ScanRepo
from vlo.storage.repo_settings import SettingsRepo
from vlo.ws.broadcaster import Broadcaster


def _probe_for(path: str, *, n_audio=2, n_subs=1) -> ProbeResult:
    import os
    is_out = path.endswith("out.mkv")
    size = os.path.getsize(path)
    p = ProbeResult(
        path=path, size_bytes=size, duration_s=10.0, width=1920, height=1080, fps=24.0,
        vcodec="hevc" if is_out else "h264",
        pix_fmt="yuv420p10le" if is_out else "yuv420p",
        is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=5_000_000, overall_bitrate_bps=5_000_000,
    )
    from vlo.core.models import AudioTrack, SubTrack
    p.audio = [AudioTrack(index=i, codec="ac3") for i in range(n_audio)]
    p.subs = [SubTrack(index=i, codec="subrip") for i in range(n_subs)]
    return p


class FakeRunner:
    """Writes a small output file and reports one progress tick + completion."""

    def __init__(self, *, out_bytes=200, cancelled=False):
        self.out_bytes = out_bytes
        self.cancelled = cancelled

    async def run(self, args, *, duration_s, on_progress=None, cancel_event=None):
        out_path = Path(args[-1])
        if self.cancelled:
            if cancel_event is not None:
                cancel_event.set()
            return EncodeResult(cancelled=True, returncode=-1)
        if on_progress:
            on_progress(EncodeProgress(progress=0.5, out_time_s=5.0, speed=2.0, eta_s=2.5))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"o" * self.out_bytes)
        if on_progress:
            on_progress(EncodeProgress(progress=1.0, out_time_s=10.0, speed=2.0, eta_s=0.0))
        return EncodeResult(cancelled=False, returncode=0)


async def _decode_ok(_bin, _path):
    return True


def _build(tmp_path: Path, db, runner, *, decode=_decode_ok, n_subs_out=1):
    settings = Settings(
        db_path=tmp_path / "x.db",
        work_dir=tmp_path / "work",
        disk_space_margin_bytes=0,
    )
    jobs_repo = JobsRepo(db)
    scan_repo = ScanRepo(db)
    cfg_repo = SettingsRepo(db)
    bus = Broadcaster()

    def probe_path(path: str) -> ProbeResult:
        return _probe_for(path, n_subs=1 if not path.endswith("out.mkv") else n_subs_out)

    mgr = JobManager(
        settings=settings, jobs_repo=jobs_repo, scan_repo=scan_repo, settings_repo=cfg_repo,
        broadcaster=bus, probe_path=probe_path, runner=runner, decode_check=decode,
        now_fn=lambda: 1234.0,
    )
    return mgr, jobs_repo, scan_repo


def _make_source(tmp_path: Path, name="Film.mkv", size=5000) -> Path:
    nas = tmp_path / "nas" / "Movies"
    nas.mkdir(parents=True, exist_ok=True)
    src = nas / name
    src.write_bytes(b"s" * size)
    return src


def _persist_media(scan_repo: ScanRepo, src: Path) -> MediaFile:
    import os
    st = src.stat()
    probe = _probe_for(str(src))
    mf = MediaFile(
        id=None, path=str(src), size_bytes=st.st_size, mtime=st.st_mtime,
        probe=probe,
        classification=Classification(kind=MediaKind.MOVIE, title="Film"),
        score=ScoreResult(bpp_real=0.3, bpp_target=0.045, overhead_ratio=6.0,
                          est_out_bytes=2000, est_gain_bytes=3000, score=80.0),
    )
    fid = scan_repo.upsert(mf, os.path.getmtime(src))
    mf.id = fid
    return mf


async def _wait_state(jobs_repo: JobsRepo, job_id: int, state: JobState, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        job = jobs_repo.get(job_id)
        if job and job.state is state:
            return job
        if job and job.state in (JobState.FAILED, JobState.CANCELLED) and job.state is not state:
            raise AssertionError(f"job ended as {job.state} (err={job.error_message})")
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {state}")


class GatedRunner:
    """Blocks inside run() until released, to observe concurrency."""

    def __init__(self):
        self.release = asyncio.Event()
        self.concurrent = 0
        self.max_concurrent = 0

    async def run(self, args, *, duration_s, on_progress=None, cancel_event=None):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"o" * 200)
        try:
            await self.release.wait()
        finally:
            self.concurrent -= 1
        return EncodeResult(cancelled=False, returncode=0)


@pytest.mark.asyncio
async def test_two_jobs_encode_in_parallel(tmp_path, db):
    runner = GatedRunner()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, runner)  # default max_parallel = 2
    srcs = [_make_source(tmp_path, name=f"Film{i}.mkv") for i in range(2)]
    media = [_persist_media(scan_repo, s) for s in srcs]

    await mgr.start()
    mgr.enqueue(media, Codec.X265, "Light")

    # Wait until both jobs are ENCODING at the same time.
    for _ in range(250):
        encoding = [j for j in jobs_repo.list() if j.state is JobState.ENCODING]
        if len(encoding) == 2:
            break
        await asyncio.sleep(0.02)
    assert runner.max_concurrent == 2, "both encodes should run concurrently"

    runner.release.set()
    for _ in range(250):
        awaiting = [j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]
        if len(awaiting) == 2:
            break
        await asyncio.sleep(0.02)
    assert len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2
    await mgr.stop()


@pytest.mark.asyncio
async def test_filename_tag_applied_on_confirm(tmp_path, db):
    src = _make_source(tmp_path, name="Movie 1080p.mkv")
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200))
    SettingsRepo(db).set("filename_tag", " x265-VLO")
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id
    await _wait_state(jobs_repo, job_id, JobState.AWAITING_CONFIRMATION)
    await mgr.confirm(job_id)

    final = src.with_name("Movie 1080p x265-VLO.mkv")
    assert final.exists()
    assert not src.exists()  # original replaced by tagged name
    await mgr.stop()


@pytest.mark.asyncio
async def test_success_then_confirm_replaces_original(tmp_path, db):
    src = _make_source(tmp_path)
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200))
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id

    job = await _wait_state(jobs_repo, job_id, JobState.AWAITING_CONFIRMATION)
    assert job.validation_json is not None
    assert Path(job.out_path_local).exists()

    await mgr.confirm(job_id)
    done = jobs_repo.get(job_id)
    assert done.state is JobState.DONE
    # Original replaced in place (same .mkv name), now ~200 bytes.
    assert src.exists() and src.stat().st_size == 200
    # Cache path updated.
    assert scan_repo.get_by_id(mf.id).size_bytes == 200
    # Work dir cleaned up.
    assert not Path(job.work_dir).exists()
    await mgr.stop()


@pytest.mark.asyncio
async def test_mp4_source_becomes_mkv_and_old_removed(tmp_path, db):
    src = _make_source(tmp_path, name="Film.mp4")
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200))
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id
    await _wait_state(jobs_repo, job_id, JobState.AWAITING_CONFIRMATION)
    await mgr.confirm(job_id)

    final = src.with_suffix(".mkv")
    assert final.exists()
    assert not src.exists()  # old .mp4 removed
    await mgr.stop()


@pytest.mark.asyncio
async def test_reject_keeps_original(tmp_path, db):
    src = _make_source(tmp_path, size=5000)
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200))
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id
    job = await _wait_state(jobs_repo, job_id, JobState.AWAITING_CONFIRMATION)

    mgr.reject(job_id)
    assert jobs_repo.get(job_id).state is JobState.REJECTED
    assert src.stat().st_size == 5000  # untouched
    assert not Path(job.work_dir).exists()
    await mgr.stop()


@pytest.mark.asyncio
async def test_validation_failure_marks_failed(tmp_path, db):
    src = _make_source(tmp_path)
    # Output probe will report 2 subs while source has 1 -> subtitle check fails.
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200), n_subs_out=2)
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id

    for _ in range(250):
        job = jobs_repo.get(job_id)
        if job.state is JobState.FAILED:
            break
        await asyncio.sleep(0.02)
    assert jobs_repo.get(job_id).state is JobState.FAILED
    assert "subtitle_tracks" in jobs_repo.get(job_id).error_message
    assert src.stat().st_size == 5000  # original safe
    await mgr.stop()


@pytest.mark.asyncio
async def test_missing_source_fails_with_clear_message(tmp_path, db):
    # A job whose source no longer exists must fail with a non-empty message.
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner())
    probe = ProbeResult(
        path="gone", size_bytes=5000, duration_s=10.0, width=1920, height=1080, fps=24.0,
        vcodec="h264", pix_fmt="yuv420p", is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=5_000_000, overall_bitrate_bps=5_000_000,
    )
    mf = MediaFile(
        id=None, path=str(tmp_path / "nas" / "gone.mkv"), size_bytes=5000, mtime=1.0,
        probe=probe,
        classification=Classification(kind=MediaKind.MOVIE, title="Gone"),
        score=ScoreResult(bpp_real=0.3, bpp_target=0.045, overhead_ratio=6.0,
                          est_out_bytes=2000, est_gain_bytes=3000, score=80.0),
    )
    mf.id = scan_repo.upsert(mf, 1.0)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id
    for _ in range(250):
        if jobs_repo.get(job_id).state is JobState.FAILED:
            break
        await asyncio.sleep(0.02)
    job = jobs_repo.get(job_id)
    assert job.state is JobState.FAILED
    assert job.error_message  # never empty
    assert "source file not found" in job.error_message
    await mgr.stop()


@pytest.mark.asyncio
async def test_no_gain_marks_failed(tmp_path, db):
    src = _make_source(tmp_path, size=100)
    # Output bigger than source -> size_gain check fails.
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=500))
    mf = _persist_media(scan_repo, src)

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    job_id = jobs_repo.list(batch_id=batch)[0].id
    for _ in range(250):
        if jobs_repo.get(job_id).state is JobState.FAILED:
            break
        await asyncio.sleep(0.02)
    assert jobs_repo.get(job_id).state is JobState.FAILED
    assert src.stat().st_size == 100
    await mgr.stop()
