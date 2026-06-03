"""Application state container and dependency wiring."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .logbuffer import setup_logging
from .core.enums import MediaKind
from .core.models import Classification, ProbeResult, ScoreResult
from .metadata.keywords import DEFAULT_ANIMATION_KEYWORDS, looks_like_animation
from .metadata.tmdb import TmdbClient
from .probe.service import ProbeService
from .scan.classifier import slugify
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
class ScanStatus:
    running: bool = False
    root: str | None = None
    total: int = 0
    done: int = 0
    probed: int = 0
    cached: int = 0
    errors: int = 0
    current_path: str = ""
    last_error: str | None = None


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
    scan_status: ScanStatus = field(default_factory=ScanStatus)
    _scan_cancel: bool = False

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
        """Scoring config whose output estimate uses the chosen codec+profile floor."""
        from dataclasses import replace

        base = self.scoring_config()
        profile = self.settings_repo.get_profile(profile_name)
        if profile is None:
            return base
        return replace(base, rank_floor_ratio=profile.floor_for(codec))

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
    async def run_scan(self, root: str, force: bool = False) -> ScanProgress:
        if self.scan_status.running:
            raise RuntimeError("a scan is already running")

        self._scan_cancel = False
        self.scan_status = ScanStatus(running=True, root=root)
        loop = asyncio.get_running_loop()
        config = self.scoring_config()

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
            st = self.scan_status
            st.total, st.done = p.total, p.done
            st.probed, st.cached, st.errors = p.probed, p.cached, p.errors
            st.current_path = p.current_path
            # Throttle: publish roughly every 5 files.
            if p.done % 5 == 0 or p.done == p.total:
                loop.call_soon_threadsafe(
                    self.broadcaster.publish,
                    {
                        "type": "scan_progress",
                        "total": p.total, "done": p.done,
                        "probed": p.probed, "cached": p.cached, "errors": p.errors,
                        "current_path": p.current_path,
                    },
                )

        logging.getLogger("vlo.scan").info("scan started: %s (force=%s)", root, force)
        try:
            result = await asyncio.to_thread(
                service.scan, root,
                force=force, progress_cb=progress_cb,
                should_cancel=lambda: self._scan_cancel,
            )
            logging.getLogger("vlo.scan").info(
                "scan finished: %d files (%d probed, %d cached, %d errors)",
                result.total, result.probed, result.cached, result.errors,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            self.scan_status.last_error = str(exc)
            logging.getLogger("vlo.scan").exception("scan crashed")
            raise
        finally:
            self.scan_status.running = False
            self.broadcaster.publish({"type": "scan_done", "root": root})

    def cancel_scan(self) -> None:
        self._scan_cancel = True


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
