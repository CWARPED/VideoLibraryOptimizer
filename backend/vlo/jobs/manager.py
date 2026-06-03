"""Sequential job worker: copy in -> encode -> validate -> await -> replace.

A single background task processes one job at a time (CPU encoders saturate
all cores). Jobs that finish encoding+validation wait in
AWAITING_CONFIRMATION without blocking the worker, which moves on to the next
queued job; the user confirms/rejects them whenever they like.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

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
        self._worker: asyncio.Task | None = None
        self._stopping = False
        self._current_job_id: int | None = None
        self._cancel_event: threading.Event | None = None
        self._last_progress_sent: float = -1.0

    # --- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        requeued = self._jobs.reset_interrupted()
        if requeued:
            self._wake.set()
        self._worker = asyncio.create_task(self._run_worker(), name="vlo-job-worker")
        # If there are already-queued jobs from a previous run, kick the worker.
        if self._jobs.next_queued() is not None:
            self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

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

        self._set_state(job_id, JobState.COPYING_BACK)
        try:
            ensure_space(
                dest.parent, out_local.stat().st_size,
                margin_bytes=self._settings.disk_space_margin_bytes,
            )
            self._set_state(job_id, JobState.REPLACING)
            final = await loop.run_in_executor(None, pipeline.safe_replace, out_local, dest)
        except (OSError, DiskSpaceError) as exc:
            self._set_state(job_id, JobState.FAILED, error_message=f"replace failed: {exc}")
            raise

        # Update the cache so the new file (possibly new extension) is tracked.
        if job.media_file_id is not None:
            st = final.stat()
            self._scan.update_path(job.media_file_id, str(final), st.st_size, st.st_mtime)

        self._cleanup_workdir(job)
        self._set_state(job_id, JobState.DONE, finished_at=self._now())
        return self._jobs.get(job_id)  # type: ignore[return-value]

    def reject(self, job_id: int) -> None:
        job = self._require(job_id, JobState.AWAITING_CONFIRMATION)
        self._cleanup_workdir(job)
        self._set_state(job_id, JobState.REJECTED, finished_at=self._now())

    def cancel(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.state.is_terminal:
            return
        if job.id == self._current_job_id and self._cancel_event is not None:
            self._cancel_event.set()  # worker will finalise as CANCELLED
        elif job.state in (JobState.QUEUED, JobState.AWAITING_CONFIRMATION):
            self._cleanup_workdir(job)
            self._set_state(job_id, JobState.CANCELLED, finished_at=self._now())

    # --- worker ---------------------------------------------------------
    async def _run_worker(self) -> None:
        while not self._stopping:
            job = self._jobs.next_queued()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                # Capture the full traceback in the log buffer, and never store
                # an empty error message (some exceptions have a blank str()).
                logger.exception("job %s failed", job.id)
                msg = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
                if job.id is not None:
                    self._cleanup_workdir(self._jobs.get(job.id) or job)
                    self._set_state(
                        job.id, JobState.FAILED,
                        error_message=msg or type(exc).__name__,
                        finished_at=self._now(),
                    )
            finally:
                self._current_job_id = None
                self._cancel_event = None

    async def _process_job(self, job: Job) -> None:
        assert job.id is not None
        loop = asyncio.get_running_loop()
        self._current_job_id = job.id
        self._cancel_event = threading.Event()
        self._last_progress_sent = -1.0

        source = Path(job.source_path)
        work_root = self._work_dir()
        work = work_root / f"job_{job.id}"
        logger.info("job %s: starting (%s, %s/%s)", job.id, job.source_path,
                    job.codec.value, job.profile_name)

        # Disk-space check (need room for the local copy + the output).
        est_out = self._estimated_output(job)
        self._set_state(job.id, JobState.COPYING_IN, started_at=self._now(), work_dir=str(work))
        ensure_space(
            work_root, (job.size_src_bytes or 0) + est_out,
            margin_bytes=self._settings.disk_space_margin_bytes,
        )

        # 1) Copy the (possibly NAS) source to local work dir.
        if not source.exists():
            raise EncodeError(f"source file not found: {source}")
        try:
            local_src = await loop.run_in_executor(None, pipeline.copy_into, source, work)
        except OSError as exc:
            raise EncodeError(f"copy from source failed: {exc}") from exc
        logger.info("job %s: copied locally (%s)", job.id, local_src)

        # 2) Re-probe the local copy for accurate stream/colour info.
        try:
            src_probe = await loop.run_in_executor(None, self._probe_path, str(local_src))
        except ProbeError as exc:
            raise EncodeError(f"could not probe local copy: {exc}") from exc

        # 3) Encode.
        out_local = work / "out.mkv"
        profile = self._cfg_repo.get_profile(job.profile_name)
        params = params_for(profile, job.codec) if profile else ""
        args = build_encode_command(
            ffmpeg_bin=self._ffmpeg,
            input_path=str(local_src),
            output_path=str(out_local),
            codec=job.codec,
            crf=job.crf,
            preset=job.preset,
            probe=src_probe,
            **self._codec_param_kwarg(job.codec, params),
        )
        self._set_state(job.id, JobState.ENCODING, out_path_local=str(out_local))
        logger.info("job %s: encoding -> %s", job.id, " ".join(args))
        try:
            result = await self._runner.run(
                args,
                duration_s=src_probe.duration_s,
                on_progress=lambda p: self._on_progress(job.id, p),  # type: ignore[arg-type]
                cancel_event=self._cancel_event,
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
        # Throttle: only emit on a >=0.5% change.
        if p.progress - self._last_progress_sent < 0.005 and p.progress < 1.0:
            return
        self._last_progress_sent = p.progress
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

    def _broadcast_queue(self) -> None:
        running = self._current_job_id
        queued = [j.id for j in self._jobs.list(state=JobState.QUEUED)]
        awaiting = [j.id for j in self._jobs.list(state=JobState.AWAITING_CONFIRMATION)]
        self._bus.publish({
            "type": "queue", "running": running, "queued": queued, "awaiting": awaiting,
        })

    def _cleanup_workdir(self, job: Job) -> None:
        if job.work_dir:
            shutil.rmtree(job.work_dir, ignore_errors=True)


def _json(obj) -> str:
    import json
    return json.dumps(obj)
