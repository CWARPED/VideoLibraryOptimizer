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

    async def run(self, args, *, duration_s, on_progress=None, cancel_event=None, on_spawn=None):
        if on_spawn:
            on_spawn(999999)  # fake ffmpeg pid
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


class ArgCaptureRunner(FakeRunner):
    """FakeRunner that records the ffmpeg argument list it was called with."""

    def __init__(self):
        super().__init__(out_bytes=200)
        self.args = None

    async def run(self, args, **kwargs):
        self.args = list(args)
        return await super().run(args, **kwargs)


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

    async def run(self, args, *, duration_s, on_progress=None, cancel_event=None, on_spawn=None):
        if on_spawn:
            on_spawn(999999)  # fake ffmpeg pid (lets pause/resume find it)
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


class GatedDecode:
    """A decode-check that blocks until released, to observe validation timing."""

    def __init__(self):
        self.release = asyncio.Event()

    async def __call__(self, _bin, _path):
        await self.release.wait()
        return True


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


def test_copy_into_is_cancellable(tmp_path):
    """A set cancel event aborts the copy and removes the partial file."""
    import threading

    from vlo.jobs import pipeline

    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * (32 * 1024 * 1024))  # 32 MiB > one chunk
    dst_dir = tmp_path / "work"

    ev = threading.Event()
    ev.set()  # already cancelled -> aborts on the first chunk check
    with pytest.raises(pipeline.CopyCancelled):
        pipeline.copy_into(src, dst_dir, ev)
    assert not (dst_dir / "big.bin").exists()  # partial file cleaned up


def test_copy_into_completes_without_cancel(tmp_path):
    from vlo.jobs import pipeline

    src = tmp_path / "a.bin"
    src.write_bytes(b"hello world" * 1000)
    out = pipeline.copy_into(src, tmp_path / "work")
    assert out.read_bytes() == src.read_bytes()


@pytest.mark.asyncio
async def test_pause_resume_state_transitions(tmp_path, db, monkeypatch):
    """pause() suspends + marks PAUSED; resume() resumes + marks ENCODING."""
    from vlo.jobs import manager as mgr_mod

    calls = []
    monkeypatch.setattr(mgr_mod, "suspend_process", lambda pid: calls.append(("suspend", pid)) or True)
    monkeypatch.setattr(mgr_mod, "resume_process", lambda pid: calls.append(("resume", pid)) or True)

    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner())
    src = _make_source(tmp_path)
    mf = _persist_media(scan_repo, src)
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    jid = jobs_repo.list(batch_id=batch)[0].id

    # Simulate an in-flight encode with a known ffmpeg pid.
    jobs_repo.update(jid, state=JobState.ENCODING.value)
    mgr._pids[jid] = 4242

    mgr.pause(jid)
    assert jobs_repo.get(jid).state is JobState.PAUSED
    assert ("suspend", 4242) in calls

    mgr.resume(jid)
    assert jobs_repo.get(jid).state is JobState.ENCODING
    assert ("resume", 4242) in calls


@pytest.mark.asyncio
async def test_pause_rejects_non_encoding_job(tmp_path, db):
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner())
    mf = _persist_media(scan_repo, _make_source(tmp_path))
    batch = mgr.enqueue([mf], Codec.X265, "Light")
    jid = jobs_repo.list(batch_id=batch)[0].id  # still QUEUED
    with pytest.raises(ValueError):
        mgr.pause(jid)


@pytest.mark.asyncio
async def test_pause_all_and_resume_all(tmp_path, db, monkeypatch):
    from vlo.jobs import manager as mgr_mod
    monkeypatch.setattr(mgr_mod, "suspend_process", lambda pid: True)
    monkeypatch.setattr(mgr_mod, "resume_process", lambda pid: True)

    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner())
    media = [_persist_media(scan_repo, _make_source(tmp_path, name=f"F{i}.mkv")) for i in range(3)]
    batch = mgr.enqueue(media, Codec.X265, "Light")
    ids = [j.id for j in jobs_repo.list(batch_id=batch)]
    for jid in ids:  # simulate in-flight encodes
        jobs_repo.update(jid, state=JobState.ENCODING.value)
        mgr._pids[jid] = 1000 + jid

    assert mgr.pause_all() == 3
    assert all(jobs_repo.get(jid).state is JobState.PAUSED for jid in ids)
    assert mgr.resume_all() == 3
    assert all(jobs_repo.get(jid).state is JobState.ENCODING for jid in ids)


@pytest.mark.asyncio
async def test_prefetches_next_file_while_encoding(tmp_path, db):
    """With a single encode slot, the next file is copied locally (READY) ahead."""
    SettingsRepo(db).set("max_parallel_encodes", 1)
    runner = GatedRunner()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, runner)
    media = [_persist_media(scan_repo, _make_source(tmp_path, name=f"P{i}.mkv")) for i in range(2)]

    await mgr.start()
    mgr.enqueue(media, Codec.X265, "Light")

    # One job encodes while the other is prefetched to READY (not yet encoding).
    for _ in range(250):
        vals = [j.state for j in jobs_repo.list()]
        if JobState.ENCODING in vals and JobState.READY in vals:
            break
        await asyncio.sleep(0.02)
    assert JobState.ENCODING in vals and JobState.READY in vals
    assert runner.max_concurrent == 1  # single slot respected

    runner.release.set()
    for _ in range(250):
        awaiting = [j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]
        if len(awaiting) == 2:
            break
        await asyncio.sleep(0.02)
    assert len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2
    await mgr.stop()


@pytest.mark.asyncio
async def test_cancel_ready_job_cleans_staged_copy(tmp_path, db):
    """Cancelling a prefetched (READY) job finalises it and removes the local copy."""
    SettingsRepo(db).set("max_parallel_encodes", 1)
    runner = GatedRunner()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, runner)
    media = [_persist_media(scan_repo, _make_source(tmp_path, name=f"C{i}.mkv")) for i in range(2)]

    await mgr.start()
    mgr.enqueue(media, Codec.X265, "Light")

    # Wait until one job is encoding and another is genuinely staged (READY and
    # not the one just claimed for encode, which is briefly READY during probe).
    ready_id = None
    for _ in range(250):
        jobs = jobs_repo.list()
        enc = [j for j in jobs if j.state is JobState.ENCODING]
        rdy = [j for j in jobs if j.state is JobState.READY]
        if enc and rdy:
            ready_id = rdy[0].id
            break
        await asyncio.sleep(0.02)
    assert ready_id is not None
    workdir = Path(jobs_repo.get(ready_id).work_dir)
    assert workdir.exists()

    mgr.cancel(ready_id)
    assert jobs_repo.get(ready_id).state is JobState.CANCELLED
    assert not workdir.exists()  # staged local copy cleaned up

    runner.release.set()
    await mgr.stop()


@pytest.mark.asyncio
async def test_validation_does_not_block_next_encode(tmp_path, db):
    """With one encode slot, a job in validation must not hold up the next encode."""
    SettingsRepo(db).set("max_parallel_encodes", 1)
    gate = GatedDecode()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner(out_bytes=200), decode=gate)
    media = [_persist_media(scan_repo, _make_source(tmp_path, name=f"V{i}.mkv")) for i in range(2)]

    await mgr.start()
    mgr.enqueue(media, Codec.X265, "Light")

    # Both jobs pass ffmpeg and sit in VALIDATING together: the single encode slot
    # was freed at ffmpeg exit rather than held through the (gated) validation.
    for _ in range(250):
        n = len([j for j in jobs_repo.list() if j.state is JobState.VALIDATING])
        if n == 2:
            break
        await asyncio.sleep(0.02)
    assert len([j for j in jobs_repo.list() if j.state is JobState.VALIDATING]) == 2

    gate.release.set()
    for _ in range(250):
        if len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2:
            break
        await asyncio.sleep(0.02)
    assert len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2
    await mgr.stop()


@pytest.mark.asyncio
async def test_pause_all_holds_future_jobs(tmp_path, db, monkeypatch):
    """Global pause suspends the running encode AND blocks new jobs from starting."""
    from vlo.jobs import manager as mgr_mod
    monkeypatch.setattr(mgr_mod, "suspend_process", lambda pid: True)
    monkeypatch.setattr(mgr_mod, "resume_process", lambda pid: True)

    runner = GatedRunner()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, runner)  # max_parallel = 2 (free slot)
    mf1 = _persist_media(scan_repo, _make_source(tmp_path, name="H0.mkv"))

    await mgr.start()
    mgr.enqueue([mf1], Codec.X265, "Light")
    first = jobs_repo.list()[0].id
    await _wait_state(jobs_repo, first, JobState.ENCODING)

    mgr.pause_all()
    assert mgr.is_paused()
    assert jobs_repo.get(first).state is JobState.PAUSED

    # A brand-new job enqueued while paused stays QUEUED even though a slot is free.
    mf2 = _persist_media(scan_repo, _make_source(tmp_path, name="H1.mkv"))
    mgr.enqueue([mf2], Codec.X265, "Light")
    second = next(j.id for j in jobs_repo.list() if j.id != first)
    await asyncio.sleep(0.2)
    assert jobs_repo.get(second).state is JobState.QUEUED  # held by the global pause

    mgr.resume_all()
    assert not mgr.is_paused()
    runner.release.set()
    for _ in range(250):
        if len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2:
            break
        await asyncio.sleep(0.02)
    assert len([j for j in jobs_repo.list() if j.state is JobState.AWAITING_CONFIRMATION]) == 2
    await mgr.stop()


@pytest.mark.asyncio
async def test_eight_bit_persists_and_reaches_ffmpeg(tmp_path, db):
    """The per-job 8-bit choice survives the DB round-trip and hits the encoder."""
    runner = ArgCaptureRunner()
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, runner)
    mf = _persist_media(scan_repo, _make_source(tmp_path))

    await mgr.start()
    batch = mgr.enqueue([mf], Codec.SVTAV1, "Light", eight_bit=True)
    jid = jobs_repo.list(batch_id=batch)[0].id
    for _ in range(250):
        if runner.args is not None:
            break
        await asyncio.sleep(0.02)
    assert runner.args is not None
    assert jobs_repo.get(jid).eight_bit is True  # persisted through the DB
    i = runner.args.index("-pix_fmt")
    assert runner.args[i + 1] == "yuv420p"  # 8-bit pixel format
    await mgr.stop()


@pytest.mark.asyncio
async def test_cancel_all_stops_queued(tmp_path, db):
    mgr, jobs_repo, scan_repo = _build(tmp_path, db, FakeRunner())  # dispatcher not started
    media = [_persist_media(scan_repo, _make_source(tmp_path, name=f"Q{i}.mkv")) for i in range(2)]
    mgr.enqueue(media, Codec.X265, "Light")
    assert mgr.cancel_all() == 2
    assert all(j.state is JobState.CANCELLED for j in jobs_repo.list())
