"""Tests for the recursive scanner: walk, caching, and exclusion handling."""

from __future__ import annotations

from pathlib import Path

from vlo.core.enums import Codec, JobState, MediaKind
from vlo.core.errors import ProbeError
from vlo.core.models import Classification, Job, MediaFile, ProbeResult, ScoreResult
from vlo.scan.scanner import ScanService, iter_video_files
from vlo.storage.repo_jobs import JobsRepo
from vlo.storage.repo_scan import ScanRepo


def _make_library(root: Path) -> None:
    (root / "Movies").mkdir(parents=True)
    (root / "Series" / "Dark" / "Season 01").mkdir(parents=True)
    (root / "Movies" / "Inception (2010) 1080p.mkv").write_bytes(b"x" * 1000)
    (root / "Movies" / "poster.jpg").write_bytes(b"y" * 10)  # non-video, ignored
    (root / "Series" / "Dark" / "Season 01" / "Dark.S01E01.mkv").write_bytes(b"z" * 2000)
    (root / "Series" / "Dark" / "Season 01" / "Dark.S01E02.mkv").write_bytes(b"z" * 2000)


def _probe_ok(path: str, size: int) -> ProbeResult:
    return ProbeResult(
        path=path, size_bytes=size, duration_s=3600.0, width=1920, height=1080,
        fps=24.0, vcodec="h264", pix_fmt="yuv420p", is_hdr=False, is_dolby_vision=False,
        video_bitrate_bps=15_000_000, overall_bitrate_bps=15_000_000, raw_json="{}",
    )


def _score_ok(probe: ProbeResult, c: Classification) -> ScoreResult:
    return ScoreResult(
        bpp_real=0.3, bpp_target=0.045, overhead_ratio=6.0,
        est_out_bytes=probe.size_bytes // 2, est_gain_bytes=probe.size_bytes // 2,
        score=80.0,
    )


def test_iter_video_files_finds_only_videos(tmp_path: Path):
    _make_library(tmp_path)
    paths = {Path(e.path).name for e in iter_video_files(str(tmp_path))}
    assert paths == {"Inception (2010) 1080p.mkv", "Dark.S01E01.mkv", "Dark.S01E02.mkv"}


def test_scan_probes_then_caches(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)
    calls = {"probe": 0}

    def counting_probe(path: str, size: int) -> ProbeResult:
        calls["probe"] += 1
        return _probe_ok(path, size)

    svc = ScanService(repo, counting_probe, _score_ok, clock)

    first = svc.scan(str(tmp_path))
    assert first.total == 3
    assert first.probed == 3
    assert first.cached == 0
    assert calls["probe"] == 3

    # Second scan: nothing changed -> all cached, no new probes.
    second = svc.scan(str(tmp_path))
    assert second.cached == 3
    assert second.probed == 0
    assert calls["probe"] == 3

    movies = repo.list_movies()
    assert len(movies) == 1
    assert movies[0].classification.title == "Inception"

    series = repo.list_series_summary()
    assert len(series) == 1
    assert series[0]["series_slug"] == "dark"
    assert series[0]["n_episodes"] == 2
    assert series[0]["content_type"] == "live_action"  # type surfaced on the series list
    assert series[0]["is_anime"] is False


def test_delete_by_kind_clears_only_that_kind(tmp_path: Path, db, clock):
    from vlo.core.enums import MediaKind

    _make_library(tmp_path)
    repo = ScanRepo(db)
    ScanService(repo, _probe_ok, _score_ok, clock).scan(str(tmp_path))
    assert len(repo.list_movies()) == 1
    assert len(repo.list_series_summary()) == 1

    # Clearing series removes only the episodes; the movie stays cached.
    assert repo.delete_by_kind(MediaKind.EPISODE) == 2
    assert repo.list_series_summary() == []
    assert len(repo.list_movies()) == 1

    # Clearing movies empties the rest.
    assert repo.delete_by_kind(MediaKind.MOVIE) == 1
    assert repo.list_movies() == []


def test_changed_file_is_reprobed(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)
    calls = {"probe": 0}

    def counting_probe(path: str, size: int) -> ProbeResult:
        calls["probe"] += 1
        return _probe_ok(path, size)

    svc = ScanService(repo, counting_probe, _score_ok, clock)
    svc.scan(str(tmp_path))
    assert calls["probe"] == 3

    # Grow one file -> size changes -> only that one is re-probed.
    (tmp_path / "Movies" / "Inception (2010) 1080p.mkv").write_bytes(b"x" * 5000)
    svc.scan(str(tmp_path))
    assert calls["probe"] == 4


def test_probe_error_marks_excluded(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)

    def failing_probe(path: str, size: int) -> ProbeResult:
        raise ProbeError("corrupt")

    svc = ScanService(repo, failing_probe, _score_ok, clock)
    result = svc.scan(str(tmp_path))
    assert result.errors == 3
    # Excluded files are persisted with a reason, not silently dropped.
    mf = repo.get_by_path(str(tmp_path / "Movies" / "Inception (2010) 1080p.mkv"))
    assert mf is not None
    assert mf.score.excluded_reason.startswith("probe failed")


def test_unexpected_error_does_not_kill_scan(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)
    calls = {"n": 0}

    def flaky_probe(path: str, size: int) -> ProbeResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("weird non-ProbeError boom")  # must not crash the scan
        return _probe_ok(path, size)

    svc = ScanService(repo, flaky_probe, _score_ok, clock)
    result = svc.scan(str(tmp_path))
    # All three files were visited; the bad one is recorded as an error.
    assert result.done == 3
    assert result.errors == 1
    assert result.probed == 2
    excluded = [m for m in repo.list_movies(only_candidates=False)
                if m.score and m.score.excluded_reason]
    series_excluded = [m for m in repo.list_episodes("dark")
                       if m.score and m.score.excluded_reason]
    assert len(excluded) + len(series_excluded) == 1


def test_deleted_file_removed_from_cache(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)
    svc = ScanService(repo, _probe_ok, _score_ok, clock)
    svc.scan(str(tmp_path))
    assert len(repo.list_episodes("dark")) == 2

    (tmp_path / "Series" / "Dark" / "Season 01" / "Dark.S01E02.mkv").unlink()
    svc.scan(str(tmp_path))
    assert len(repo.list_episodes("dark")) == 1


# --- delete_missing vs. job foreign keys --------------------------------
def _insert_media(repo: ScanRepo, path: str, now: float = 1.0) -> int:
    mf = MediaFile(
        id=None, path=path, size_bytes=1000, mtime=1.0,
        probe=ProbeResult(
            path=path, size_bytes=1000, duration_s=10.0, width=1920, height=1080, fps=24.0,
            vcodec="h264", pix_fmt="yuv420p", is_hdr=False, is_dolby_vision=False,
            video_bitrate_bps=15_000_000, overall_bitrate_bps=15_000_000, raw_json="{}",
        ),
        classification=Classification(kind=MediaKind.MOVIE, title="X"),
        score=ScoreResult(bpp_real=0.3, bpp_target=0.045, overhead_ratio=6.0,
                          est_out_bytes=500, est_gain_bytes=500, score=80.0),
    )
    return repo.upsert(mf, now)


def _insert_job(jobs: JobsRepo, media_file_id: int, state: JobState) -> int:
    jid = jobs.create(Job(
        id=None, media_file_id=media_file_id, source_path="/old/path.mkv",
        codec=Codec.X265, profile_name="Light", crf=20, preset="slow",
        state=JobState.QUEUED, size_src_bytes=1000, created_at=1.0,
    ))
    jobs.update(jid, state=state)
    return jid


def test_delete_missing_does_not_crash_on_job_fk(db):
    """Regression: deleting a media row referenced by a job must not raise."""
    repo, jobs = ScanRepo(db), JobsRepo(db)
    fid = _insert_media(repo, "/lib/gone.mkv")
    _insert_job(jobs, fid, JobState.DONE)
    # File no longer present -> previously raised FOREIGN KEY constraint failed.
    removed = repo.delete_missing(present_paths=set())
    assert removed == 1
    assert repo.get_by_id(fid) is None


def test_delete_missing_nulls_terminal_job_link(db):
    repo, jobs = ScanRepo(db), JobsRepo(db)
    fid = _insert_media(repo, "/lib/gone.mkv")
    jid = _insert_job(jobs, fid, JobState.FAILED)
    repo.delete_missing(present_paths=set())
    job = jobs.get(jid)
    assert job is not None  # history preserved
    assert job.media_file_id is None  # link detached
    assert job.source_path == "/old/path.mkv"


def test_delete_missing_keeps_rows_with_active_jobs(db):
    repo, jobs = ScanRepo(db), JobsRepo(db)
    fid = _insert_media(repo, "/lib/in_progress.mkv")
    _insert_job(jobs, fid, JobState.AWAITING_CONFIRMATION)
    removed = repo.delete_missing(present_paths=set())
    assert removed == 0
    assert repo.get_by_id(fid) is not None  # kept for the active job


def test_delete_missing_removes_rows_without_jobs(db):
    repo = ScanRepo(db)
    fid = _insert_media(repo, "/lib/orphan.mkv")
    removed = repo.delete_missing(present_paths=set())
    assert removed == 1
    assert repo.get_by_id(fid) is None


def test_delete_missing_scoped_to_root_keeps_other_folders(db):
    """Scanning one folder must never wipe the cache of another folder."""
    repo = ScanRepo(db)
    fid_films = _insert_media(repo, r"D:\Films\gone.mkv")
    fid_series = _insert_media(repo, r"D:\Series\kept.mkv")
    removed = repo.delete_missing(present_paths=set(), root=r"D:\Films")
    assert removed == 1
    assert repo.get_by_id(fid_films) is None
    assert repo.get_by_id(fid_series) is not None  # other root untouched


def test_delete_missing_root_scope_is_not_a_prefix_match(db):
    """D:\\Films must not swallow D:\\Films HD (path prefix != folder)."""
    repo = ScanRepo(db)
    fid = _insert_media(repo, r"D:\Films HD\kept.mkv")
    removed = repo.delete_missing(present_paths=set(), root=r"D:\Films")
    assert removed == 0
    assert repo.get_by_id(fid) is not None


# --- parallel scan -------------------------------------------------------
def test_parallel_scan_probes_all_files(tmp_path: Path, db, clock):
    import threading

    _make_library(tmp_path)
    repo = ScanRepo(db)
    lock = threading.Lock()
    calls = {"probe": 0}

    def counting_probe(path: str, size: int) -> ProbeResult:
        with lock:
            calls["probe"] += 1
        return _probe_ok(path, size)

    svc = ScanService(repo, counting_probe, _score_ok, clock)
    first = svc.scan(str(tmp_path), workers=4)
    assert first.total == 3
    assert first.done == 3
    assert first.probed == 3
    assert calls["probe"] == 3

    # Second parallel scan: cache hits, no new probes.
    second = svc.scan(str(tmp_path), workers=4)
    assert second.cached == 3
    assert calls["probe"] == 3
    assert len(repo.list_movies()) == 1


def test_parallel_scan_survives_probe_errors(tmp_path: Path, db, clock):
    _make_library(tmp_path)
    repo = ScanRepo(db)

    def failing_probe(path: str, size: int) -> ProbeResult:
        raise ProbeError("corrupt")

    result = ScanService(repo, failing_probe, _score_ok, clock).scan(
        str(tmp_path), workers=4
    )
    assert result.done == 3
    assert result.errors == 3
