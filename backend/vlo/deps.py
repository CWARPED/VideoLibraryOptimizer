"""Application state container and dependency wiring."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .logbuffer import setup_logging
from .core.enums import MediaKind
from .core.errors import ProbeError
from .core.models import Classification, ProbeResult, ScoreResult
from .metadata.keywords import DEFAULT_ANIMATION_KEYWORDS, looks_like_animation
from .metadata.tmdb import TmdbClient
from .probe.service import ProbeService
from .scan.classifier import classify, slugify
from .scan.scanner import ScanProgress, ScanService
from .scoring.score import ScoringConfig, compute_score
from .storage.db import Database
from .storage.repo_jobs import JobsRepo
from .storage.repo_meta import CachedGenre, MetadataRepo
from .storage.repo_scan import ScanRepo
from .storage.repo_settings import SettingsRepo
from .jobs.manager import JobManager
from .ws.broadcaster import Broadcaster

ContentFn = Callable[[str, Classification], tuple[str, bool, str]]


@dataclass(slots=True)
class ScanSession:
    """One scan run (several can be active at once, on different roots)."""

    id: str
    root: str
    force: bool = False
    running: bool = True
    total: int = 0
    done: int = 0
    probed: int = 0
    cached: int = 0
    errors: int = 0
    current_path: str = ""
    last_error: str | None = None
    cancel: bool = False

    def to_dict(self) -> dict:
        return {
            "scan_id": self.id,
            "root": self.root,
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "probed": self.probed,
            "cached": self.cached,
            "errors": self.errors,
            "current_path": self.current_path,
            "last_error": self.last_error,
        }


_MAX_FINISHED_SESSIONS = 10  # finished scans kept for the UI history


@dataclass
class AppState:
    settings: Settings
    db: Database
    scan_repo: ScanRepo
    jobs_repo: JobsRepo
    settings_repo: SettingsRepo
    metadata_repo: MetadataRepo
    probe_service: ProbeService
    broadcaster: Broadcaster
    job_manager: JobManager
    scans: dict[str, ScanSession] = field(default_factory=dict)

    # --- scoring config (persisted settings override env defaults) ------
    def scoring_config(self) -> ScoringConfig:
        s = self.settings
        repo = self.settings_repo
        return ScoringConfig(
            bands=repo.reference_bands("live_action"),
            animation_bands=repo.reference_bands("animation"),
            weight_overhead=repo.get("weight_overhead", s.weight_overhead),
            weight_gain=repo.get("weight_gain", s.weight_gain),
            gain_ref_gb=repo.get("gain_ref_gb", s.gain_ref_gb),
            min_overhead_ratio=repo.get("min_overhead_ratio", s.min_overhead_ratio),
            exclude_dolby_vision=repo.get("exclude_dolby_vision", s.exclude_dolby_vision),
        )

    def scoring_config_for(self, codec, profile_name: str) -> ScoringConfig:
        """Scoring config whose output estimate is computed for the chosen codec+CRF."""
        from dataclasses import replace

        base = self.scoring_config()
        profile = self.settings_repo.get_profile(profile_name)
        if profile is None:
            return base
        return replace(base, rank_codec=codec, rank_crf=profile.crf_for(codec))

    # --- content-type resolution (TMDB -> cache -> keyword -> default) --
    def make_content_resolver(self) -> ContentFn:
        """Build a content-type resolver from current settings (used per scan)."""
        repo = self.settings_repo
        api_key = repo.get("tmdb_api_key", self.settings.tmdb_api_key) or ""
        enabled = repo.get("tmdb_enabled", self.settings.tmdb_enabled)
        keywords = repo.get("animation_keywords", DEFAULT_ANIMATION_KEYWORDS)
        tmdb = TmdbClient(api_key) if (enabled and api_key) else None

        def resolve(path: str, c: Classification) -> tuple[str, bool, str]:
            # 1) Manual override is locked.
            existing = self.scan_repo.get_by_path(path)
            if existing and existing.classification and \
                    existing.classification.content_source == "manual":
                cc = existing.classification
                return cc.content_type, cc.is_anime, "manual"

            is_series = c.kind is MediaKind.EPISODE
            if is_series:
                slug = c.series_slug or slugify(c.series_title or c.title or "")
                key, query, year, kind = f"tv:{slug}", (c.series_title or c.title), None, "tv"
            else:
                key = f"movie:{slugify(c.title or '')}:{c.year or ''}"
                query, year, kind = c.title, c.year, "movie"

            # 2) Cache.
            cached = self.metadata_repo.get(key)
            if cached is not None:
                return cached.content_type, cached.is_anime, "tmdb"

            # 3) TMDB.
            if tmdb is not None and query:
                info = tmdb.lookup(query, year, kind)
                if info is not None:
                    self.metadata_repo.set(
                        key,
                        CachedGenre(info.content_type, info.is_anime, info.genres, info.tmdb_id),
                        time.time(),
                    )
                    return info.content_type, info.is_anime, "tmdb"

            # 4) Keyword fallback. 5) default.
            if looks_like_animation(path, keywords):
                return "animation", False, "keyword"
            return "live_action", False, "default"

        return resolve

    # --- scan orchestration --------------------------------------------
    def start_scan(self, root: str, force: bool = False) -> ScanSession:
        """Register a new scan session and run it in the background.

        Several scans may run in parallel, but never two on the same root.
        """
        session = self._new_session(root, force)
        asyncio.create_task(self._run_scan_session(session))
        return session

    async def run_scan(self, root: str, force: bool = False) -> ScanProgress:
        """Run a scan to completion (awaitable variant of :meth:`start_scan`)."""
        return await self._run_scan_session(self._new_session(root, force))

    def _new_session(self, root: str, force: bool) -> ScanSession:
        norm = os.path.normcase(os.path.abspath(root))
        for s in self.scans.values():
            if s.running and os.path.normcase(os.path.abspath(s.root)) == norm:
                raise RuntimeError("ce dossier est déjà en cours de scan")
        # Cap the finished-session history so the dict cannot grow unbounded.
        finished = [sid for sid, s in self.scans.items() if not s.running]
        for sid in finished[:-_MAX_FINISHED_SESSIONS]:
            del self.scans[sid]
        session = ScanSession(id=uuid.uuid4().hex[:8], root=root, force=force)
        self.scans[session.id] = session
        return session

    async def _run_scan_session(self, session: ScanSession) -> ScanProgress:
        loop = asyncio.get_running_loop()
        config = self.scoring_config()
        workers = max(1, int(self.settings_repo.get("scan_workers", self.settings.scan_workers)))

        def score_fn(probe: ProbeResult, c: Classification) -> ScoreResult:
            return compute_score(probe, c, config)

        service = ScanService(
            self.scan_repo,
            probe_fn=self.probe_service.probe,
            score_fn=score_fn,
            now_fn=time.time,
            content_fn=self.make_content_resolver(),
        )

        def progress_cb(p: ScanProgress) -> None:
            session.total, session.done = p.total, p.done
            session.probed, session.cached, session.errors = p.probed, p.cached, p.errors
            session.current_path = p.current_path
            # Throttle: publish roughly every 5 files.
            if p.done % 5 == 0 or p.done == p.total:
                loop.call_soon_threadsafe(
                    self.broadcaster.publish,
                    {
                        "type": "scan_progress",
                        "scan_id": session.id, "root": session.root,
                        "total": p.total, "done": p.done,
                        "probed": p.probed, "cached": p.cached, "errors": p.errors,
                        "current_path": p.current_path,
                    },
                )

        logging.getLogger("vlo.scan").info(
            "scan %s started: %s (force=%s, workers=%d)",
            session.id, session.root, session.force, workers,
        )
        try:
            result = await asyncio.to_thread(
                service.scan, session.root,
                force=session.force, progress_cb=progress_cb,
                should_cancel=lambda: session.cancel,
                workers=workers,
            )
            logging.getLogger("vlo.scan").info(
                "scan %s finished: %d files (%d probed, %d cached, %d errors)",
                session.id, result.total, result.probed, result.cached, result.errors,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            session.last_error = str(exc)
            logging.getLogger("vlo.scan").exception("scan %s crashed", session.id)
            raise
        finally:
            session.running = False
            self.broadcaster.publish(
                {"type": "scan_done", "scan_id": session.id, "root": session.root,
                 "errors": session.errors, "last_error": session.last_error}
            )

    def cancel_scan(self, scan_id: str | None = None) -> bool:
        """Cancel one scan by id, or every running scan when no id is given."""
        hit = False
        for s in self.scans.values():
            if s.running and (scan_id is None or s.id == scan_id):
                s.cancel = True
                hit = True
        return hit

    # --- post-processing cache refresh ---------------------------------
    async def refresh_media_file(self, file_id: int) -> None:
        """Re-probe and re-score a file after it was re-encoded in place.

        Without this, the cache keeps the source's old bitrate/score and the
        file keeps showing up as a heavy candidate until the next full scan.
        Preserves the manual/TMDB content type.
        """
        mf = self.scan_repo.get_by_id(file_id)
        if mf is None or not os.path.exists(mf.path):
            return
        try:
            probe = await asyncio.to_thread(self.probe_service.probe, mf.path)
        except ProbeError:
            logging.getLogger("vlo.jobs").warning("refresh: could not probe %s", mf.path)
            return

        classification = classify(mf.path)
        if mf.classification:  # keep the resolved content type (manual/tmdb)
            classification.content_type = mf.classification.content_type
            classification.is_anime = mf.classification.is_anime
            classification.content_source = mf.classification.content_source
        score = compute_score(probe, classification, self.scoring_config())

        st = os.stat(mf.path)
        mf.probe = probe
        mf.classification = classification
        mf.score = score
        mf.size_bytes = st.st_size
        mf.mtime = st.st_mtime
        mf.reencoded_at = time.time()  # mark as processed -> no longer proposed
        self.scan_repo.upsert(mf, time.time())
        self.broadcaster.publish({"type": "media_updated", "id": file_id})
        logging.getLogger("vlo.jobs").info(
            "refresh: %s rescored (overhead %.2f, %s)",
            mf.path, score.overhead_ratio, score.excluded_reason or "candidate",
        )


def build_app_state(settings: Settings | None = None) -> AppState:
    settings = settings or get_settings()
    setup_logging()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    scan_repo = ScanRepo(db)
    jobs_repo = JobsRepo(db)
    settings_repo = SettingsRepo(db)
    metadata_repo = MetadataRepo(db)
    _, ffprobe = settings.resolve_binaries()
    probe_service = ProbeService(ffprobe)
    broadcaster = Broadcaster()
    job_manager = JobManager(
        settings=settings,
        jobs_repo=jobs_repo,
        scan_repo=scan_repo,
        settings_repo=settings_repo,
        broadcaster=broadcaster,
        probe_path=probe_service.probe,
    )
    return AppState(
        settings=settings,
        db=db,
        scan_repo=scan_repo,
        jobs_repo=jobs_repo,
        settings_repo=settings_repo,
        metadata_repo=metadata_repo,
        probe_service=probe_service,
        broadcaster=broadcaster,
        job_manager=job_manager,
    )
