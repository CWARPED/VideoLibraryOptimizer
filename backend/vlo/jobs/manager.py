"""Concurrent job workers: copy in -> encode -> validate -> await -> replace.

Up to ``max_parallel_encodes`` jobs run at once. A single 1080p x265/AV1 encode
under-uses a many-core CPU (frame-parallelism ceiling), so running 2+ in
parallel raises throughput; it also naturally overlaps the next job's NAS copy
with the ongoing encodes. Jobs that finish encoding+validation wait in
AWAITING_CONFIRMATION (off-pool) until the user confirms/rejects them.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from .. import naming
from ..config import Settings
from ..core.enums import Codec, JobState
from ..core.errors import DiskSpaceError, EncodeError, ProbeError
from ..core.models import Job, MediaFile, ProbeResult
from ..encode.ffmpeg_cmd import build_encode_command
from ..encode.profiles import params_for, resolve_encode_params
from ..encode.runner import EncodeProgress, EncodeRunner
from ..encode.validate import validate_output
from ..storage.repo_jobs import JobsRepo
from ..storage.repo_scan import ScanRepo
from ..storage.repo_settings import SettingsRepo
from ..ws.broadcaster import Broadcaster
from . import pipeline
from .diskspace import ensure_space

logger = logging.getLogger("vlo.jobs")

ProbePathFn = Callable[[str], ProbeResult]
DecodeCheckFn = Callable[[str, Path], Awaitable[bool]]


class JobManager:
    def __init__(
        self,
        *,
        settings: Settings,
        jobs_repo: JobsRepo,
        scan_repo: ScanRepo,
        settings_repo: SettingsRepo,
        broadcaster: Broadcaster,
        probe_path: ProbePathFn,
        runner: EncodeRunner | None = None,
        decode_check: DecodeCheckFn | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs_repo
        self._scan = scan_repo
        self._cfg_repo = settings_repo
        self._bus = broadcaster
        self._probe_path = probe_path
        self._runner = runner or EncodeRunner()
        self._ffmpeg, self._ffprobe = settings.resolve_binaries()
        self._decode_check = decode_check or (
            lambda _bin, p: pipeline.decode_check(self._ffmpeg, p)
        )
        import time
        self._now = now_fn or time.time

        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task | None = None
        self._stopping = False
        # job_id -> running task / per-job cancel flag / last broadcast progress
        self._active: dict[int, asyncio.Task] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._last_progress: dict[int, float] = {}

    # --- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        requeued = self._jobs.reset_interrupted()
        self._dispatcher = asyncio.create_task(self._run_dispatcher(), name="vlo-dispatcher")
        if requeued or self._jobs.next_queued() is not None:
            self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for ev in list(self._cancel_events.values()):
            ev.set()
        for task in list(self._active.values()):
            task.cancel()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass

    # --- config helpers -------------------------------------------------
    def _max_parallel(self) -> int:
        n = self._cfg_repo.get("max_parallel_encodes", self._settings.max_parallel_encodes)
        try:
            return max(1, int(n))
        except (TypeError, ValueError):
            return 1

    def _naming_settings(self) -> tuple[str, bool]:
        tag = self._cfg_repo.get("filename_tag", self._settings.filename_tag) or ""
        rewrite = self._cfg_repo.get("rewrite_codec_tags", self._settings.rewrite_codec_tags)
        return tag, bool(rewrite)

    def _output_naming(self, job: Job) -> tuple[str, str | None]:
        """Return (final_stem, title_override) for a job's output."""
        tag, rewrite = self._naming_settings()
        stem = naming.output_stem(Path(job.source_path).stem, job.codec, tag=tag, rewrite=rewrite)
        return stem, (stem if rewrite else None)

    # --- enqueue --------------------------------------------------------
    def enqueue(
        self, media_files: Sequence[MediaFile], codec: Codec, profile_name: str
    ) -> str:
        profile = self._cfg_repo.get_profile(profile_name)
        if profile is None:
            raise ValueError(f"unknown profile: {profile_name}")
        crf, preset = resolve_encode_params(profile, codec)
        batch_id = uuid.uuid4().hex
        now = self._now()
        for mf in media_files:
            job = Job(
                id=None,
                media_file_id=mf.id,
                source_path=mf.path,
                codec=codec,
                profile_name=profile_name,
                crf=crf,
                preset=preset,
                state=JobState.QUEUED,
                batch_id=batch_id,
                size_src_bytes=mf.size_bytes,
                created_at=now,
            )
            self._jobs.create(job)
        self._wake.set()
        self._broadcast_queue()
        return batch_id

    # --- user actions ---------------------------------------------------
    async def confirm(self, job_id: int) -> Job:
        job = self._require(job_id, JobState.AWAITING_CONFIRMATION)
        loop = asyncio.get_running_loop()
        dest = Path(job.source_path)
        out_local = Path(job.out_path_local) if job.out_path_local else None
        if out_local is None or not out_local.exists():
            self._set_state(job_id, JobState.FAILED, error_message="local output missing")
            raise EncodeError("local output missing for confirmation")

        final_stem, _ = self._output_naming(job)
        self._set_state(job_id, JobState.COPYING_BACK)
        try:
            ensure_space(
                dest.parent, out_local.stat().st_size,
                margin_bytes=self._settings.disk_space_margin_bytes,
            )
            self._set_state(job_id, JobState.REPLACING)
            final = await loop.run_in_executor(
                None, pipeline.safe_replace, out_local, dest, final_stem
            )
        except (OSError, DiskSpaceError) as exc:
            self._set_state(job_id, JobState.FAILED, error_message=f"replace failed: {exc}")
            raise

        # Update the cache so the new file (possibly new name) is tracked.
        if job.media_file_id is not None:
            st = final.stat()
            self._scan.update_path(job.media_file_id, str(final), st.st_size, st.st_mtime)

        self._cleanup_workdir(job)
        self._set_state(job_id, JobState.DONE, finished_at=self._now())
        self._record_gain(job)
        return self._jobs.get(job_id)  # type: ignore[return-value]

    def _record_gain(self, job: Job) -> None:
        """Accumulate persistent space-saved stats (survive queue cleanup)."""
        total = (self._cfg_repo.get("total_gain_bytes", 0) or 0) + (job.gain_bytes or 0)
        done = (self._cfg_repo.get("total_encodes_done", 0) or 0) + 1
        self._cfg_repo.set("total_gain_bytes", total)
        self._cfg_repo.set("total_encodes_done", done)
        self._bus.publish({
            "type": "stats", "total_gain_bytes": total, "total_encodes_done": done,
        })

    def reject(self, job_id: int) -> None:
        job = self._require(job_id, JobState.AWAITING_CONFIRMATION)
        self._cleanup_workdir(job)
        self._set_state(job_id, JobState.REJECTED, finished_at=self._now())

    def cancel(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.state.is_terminal:
            return
        ev = self._cancel_events.get(job_id)
        if ev is not None:
            ev.set()  # active job: copy/encode aborts -> finalised CANCELLED by the worker
            return
        if job.state in (JobState.QUEUED, JobState.AWAITING_CONFIRMATION):
            self._cleanup_workdir(job)
            self._set_state(job_id, JobState.CANCELLED, finished_at=self._now())

    # --- dispatcher -----------------------------------------------------
    async def _run_dispatcher(self) -> None:
        while not self._stopping:
            while not self._stopping and len(self._active) < self._max_parallel():
                job = self._claim_next()
                if job is None:
                    break
                ev = threading.Event()
                self._cancel_events[job.id] = ev
                task = asyncio.create_task(
                    self._process_job_safe(job, ev), name=f"vlo-job-{job.id}"
                )
                self._active[job.id] = task
                task.add_done_callback(self._make_done_cb(job.id))

            self._wake.clear()
            if len(self._active) < self._max_parallel() and self._jobs.next_queued() is not None:
                continue  # capacity freed and work waiting -> refill immediately
            await self._wake.wait()

    def _claim_next(self) -> Job | None:
        """Pick the next queued job and reserve it (so it isn't claimed twice)."""
        job = self._jobs.next_queued()
        if job is None:
            return None
        self._jobs.update(job.id, state=JobState.COPYING_IN, started_at=self._now())
        job.state = JobState.COPYING_IN
        return job

    def _make_done_cb(self, job_id: int) -> Callable[[asyncio.Task], None]:
        def cb(_task: asyncio.Task) -> None:
            self._active.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            self._last_progress.pop(job_id, None)
            self._wake.set()
        return cb

    async def _process_job_safe(self, job: Job, cancel_event: threading.Event) -> None:
        try:
            await self._process_job(job, cancel_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            logger.exception("job %s failed", job.id)
            msg = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
            self._cleanup_workdir(self._jobs.get(job.id) or job)
            self._set_state(
                job.id, JobState.FAILED,
                error_message=msg or type(exc).__name__, finished_at=self._now(),
            )

    async def _process_job(self, job: Job, cancel_event: threading.Event) -> None:
        assert job.id is not None
        loop = asyncio.get_running_loop()
        self._last_progress[job.id] = -1.0

        source = Path(job.source_path)
        work_root = self._work_dir()
        work = work_root / f"job_{job.id}"
        logger.info("job %s: starting (%s, %s/%s)", job.id, job.source_path,
                    job.codec.value, job.profile_name)

        est_out = self._estimated_output(job)
        self._set_state(job.id, JobState.COPYING_IN, work_dir=str(work))
        ensure_space(
            work_root, (job.size_src_bytes or 0) + est_out,
            margin_bytes=self._settings.disk_space_margin_bytes,
        )

        # 1) Copy the (possibly NAS) source to local work dir (cancellable mid-copy).
        if not source.exists():
            raise EncodeError(f"source file not found: {source}")
        try:
            local_src = await loop.run_in_executor(
                None, pipeline.copy_into, source, work, cancel_event
            )
        except pipeline.CopyCancelled:
            self._cleanup_workdir(job)
            self._set_state(job.id, JobState.CANCELLED, finished_at=self._now())
            return
        except OSError as exc:
            raise EncodeError(f"copy from source failed: {exc}") from exc
        logger.info("job %s: copied locally (%s)", job.id, local_src)
        if cancel_event.is_set():
            self._cleanup_workdir(job)
            self._set_state(job.id, JobState.CANCELLED, finished_at=self._now())
            return

        # 2) Re-probe the local copy for accurate stream/colour info.
        try:
            src_probe = await loop.run_in_executor(None, self._probe_path, str(local_src))
        except ProbeError as exc:
            raise EncodeError(f"could not probe local copy: {exc}") from exc

        # 3) Encode.
        out_local = work / "out.mkv"
        profile = self._cfg_repo.get_profile(job.profile_name)
        params = params_for(profile, job.codec) if profile else ""
        _, title = self._output_naming(job)
        args = build_encode_command(
            ffmpeg_bin=self._ffmpeg,
            input_path=str(local_src),
            output_path=str(out_local),
            codec=job.codec,
            crf=job.crf,
            preset=job.preset,
            probe=src_probe,
            title=title,
            **self._codec_param_kwarg(job.codec, params),
        )
        self._set_state(job.id, JobState.ENCODING, out_path_local=str(out_local))
        logger.info("job %s: encoding -> %s", job.id, " ".join(args))
        try:
            result = await self._runner.run(
                args,
                duration_s=src_probe.duration_s,
                on_progress=lambda p: self._on_progress(job.id, p),  # type: ignore[arg-type]
                cancel_event=cancel_event,
            )
        except FileNotFoundError as exc:
            raise EncodeError(f"ffmpeg not found ({self._ffmpeg}): {exc}") from exc
        if result.cancelled:
            self._cleanup_workdir(job)
            self._set_state(job.id, JobState.CANCELLED, finished_at=self._now())
            return
        if not out_local.exists():
            raise EncodeError("encode produced no output file")

        # 4) Validate.
        self._set_state(job.id, JobState.VALIDATING, progress=1.0)
        try:
            out_probe = await loop.run_in_executor(None, self._probe_path, str(out_local))
        except ProbeError as exc:
            raise EncodeError(f"could not probe encoded output: {exc}") from exc
        decoded_ok = await self._decode_check(self._ffmpeg, out_local)
        report = validate_output(
            src_probe, out_probe,
            codec=job.codec,
            duration_tolerance_pct=self._settings.duration_tolerance_pct,
            is_vfr=False,
            decoded_ok=decoded_ok,
        )
        self._jobs.update(
            job.id,
            size_out_bytes=out_probe.size_bytes,
            gain_bytes=report.gain_bytes,
            validation_json=_json(report.to_dict()),
        )
        if not report.ok:
            failed = [c.name for c in report.checks if not c.passed]
            logger.warning("job %s: validation failed (%s)", job.id, ", ".join(failed))
            self._cleanup_workdir(job)
            self._set_state(
                job.id, JobState.FAILED,
                error_message=f"validation failed: {', '.join(failed)}",
                finished_at=self._now(),
            )
            return

        # 5) Wait for the user. Output stays on local disk.
        logger.info("job %s: ready for confirmation (gain %d bytes)", job.id, report.gain_bytes)
        self._set_state(job.id, JobState.AWAITING_CONFIRMATION)

    # --- helpers --------------------------------------------------------
    def _work_dir(self) -> Path:
        """Work directory: persisted setting overrides the env/default."""
        override = self._cfg_repo.get("work_dir")
        path = Path(override) if override else self._settings.work_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _codec_param_kwarg(codec: Codec, params: str) -> dict[str, str]:
        if not params:
            return {}
        return {"x265_params": params} if codec is Codec.X265 else {"svtav1_params": params}

    def _estimated_output(self, job: Job) -> int:
        if job.media_file_id is not None:
            mf = self._scan.get_by_id(job.media_file_id)
            if mf and mf.score and mf.score.est_out_bytes:
                return mf.score.est_out_bytes
        return job.size_src_bytes or 0

    def _on_progress(self, job_id: int, p: EncodeProgress) -> None:
        # Throttle: only emit on a >=0.5% change (per job).
        last = self._last_progress.get(job_id, -1.0)
        if p.progress - last < 0.005 and p.progress < 1.0:
            return
        self._last_progress[job_id] = p.progress
        speed = f"{p.speed:.2f}x" if p.speed else None
        self._jobs.update(job_id, progress=p.progress, speed=speed, eta_s=p.eta_s)
        self._bus.publish({
            "type": "job_progress",
            "job_id": job_id,
            "state": JobState.ENCODING.value,
            "progress": round(p.progress, 4),
            "speed": speed,
            "eta_s": p.eta_s,
        })

    def _require(self, job_id: int, expected: JobState) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if job.state is not expected:
            raise ValueError(f"job {job_id} is {job.state.value}, expected {expected.value}")
        return job

    def _set_state(self, job_id: int, state: JobState, **fields) -> None:
        self._jobs.update(job_id, state=state, **fields)
        job = self._jobs.get(job_id)
        payload = {"type": "job_state", "job_id": job_id, "state": state.value}
        if job and job.validation_json and state == JobState.AWAITING_CONFIRMATION:
            payload["validation"] = job.validation_json
        if fields.get("error_message"):
            payload["error"] = fields["error_message"]
        self._bus.publish(payload)
        self._broadcast_queue()

    def has_active(self) -> bool:
        """True if at least one job is currently encoding (binary in use)."""
        return bool(self._active)

    def _broadcast_queue(self) -> None:
        self._bus.publish({
            "type": "queue",
            "running": list(self._active.keys()),
            "queued": [j.id for j in self._jobs.list(state=JobState.QUEUED)],
            "awaiting": [j.id for j in self._jobs.list(state=JobState.AWAITING_CONFIRMATION)],
        })

    def _cleanup_workdir(self, job: Job) -> None:
        if job.work_dir:
            shutil.rmtree(job.work_dir, ignore_errors=True)


def _json(obj) -> str:
    import json
    return json.dumps(obj)
