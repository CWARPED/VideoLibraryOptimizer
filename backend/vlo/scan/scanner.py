"""Recursive library scanner with mtime/size-based caching.

Walks every subfolder of a root, and for each video file either reuses the
cached probe (when size+mtime are unchanged) or probes/classifies/scores it
afresh. Probe and score functions are injected so the walk logic stays
testable without ffprobe.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..config import VIDEO_EXTENSIONS
from ..core.errors import ProbeError
from ..core.models import Classification, MediaFile, ProbeResult, ScoreResult
from ..storage.repo_scan import ScanRepo
from .classifier import classify

logger = logging.getLogger("vlo.scan")

# Injected callables.
ProbeFn = Callable[[str, int], ProbeResult]  # (path, size) -> ProbeResult
ScoreFn = Callable[[ProbeResult, Classification], ScoreResult]
ProgressFn = Callable[["ScanProgress"], None]
# (path, classification) -> (content_type, is_anime, source)
ContentFn = Callable[[str, Classification], tuple[str, bool, str]]


@dataclass(slots=True)
class FileEntry:
    path: str
    size_bytes: int
    mtime: float


@dataclass(slots=True)
class ScanProgress:
    total: int
    done: int
    current_path: str
    probed: int
    cached: int
    errors: int


def iter_video_files(root: str) -> Iterator[FileEntry]:
    """Yield every video file under ``root`` (recursive), with size and mtime."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield FileEntry(path=full, size_bytes=st.st_size, mtime=st.st_mtime)


class ScanService:
    def __init__(
        self,
        repo: ScanRepo,
        probe_fn: ProbeFn,
        score_fn: ScoreFn,
        now_fn: Callable[[], float],
        content_fn: ContentFn | None = None,
    ) -> None:
        self._repo = repo
        self._probe = probe_fn
        self._score = score_fn
        self._now = now_fn
        self._content = content_fn

    def scan(
        self,
        root: str,
        *,
        force: bool = False,
        progress_cb: ProgressFn | None = None,
        should_cancel: Callable[[], bool] | None = None,
        workers: int = 1,
    ) -> ScanProgress:
        """Walk ``root`` and probe/score every video file.

        ``workers > 1`` probes files concurrently in a thread pool (ffprobe is
        I/O + subprocess bound); DB writes stay serialised by the DB lock.
        """
        entries = list(iter_video_files(root))
        total = len(entries)
        progress = ScanProgress(total=total, done=0, current_path="", probed=0, cached=0, errors=0)
        present = {e.path for e in entries}

        def cancelled() -> bool:
            return bool(should_cancel and should_cancel())

        if workers <= 1:
            for entry in entries:
                if cancelled():
                    break
                self._handle_entry(entry, force, progress, progress_cb, None)
        else:
            lock = threading.Lock()
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="vlo-scan"
            ) as pool:
                def worker(entry: FileEntry) -> None:
                    if cancelled():
                        return
                    self._handle_entry(entry, force, progress, progress_cb, lock)

                for _ in pool.map(worker, entries):
                    pass

        # Drop cache rows (under this root only) for files that no longer
        # exist — never on a cancelled scan, and a failure here must never
        # lose an otherwise-completed scan.
        if not cancelled():
            try:
                self._repo.delete_missing(present, root=root)
            except Exception:  # noqa: BLE001
                logger.exception("delete_missing failed (scan results kept)")
        return progress

    def _handle_entry(
        self,
        entry: FileEntry,
        force: bool,
        progress: ScanProgress,
        progress_cb: ProgressFn | None,
        lock: threading.Lock | None,
    ) -> None:
        outcome = "cached"
        if force or not self._repo.cache_is_fresh(entry.path, entry.size_bytes, entry.mtime):
            try:
                self._process(entry)
                outcome = "probed"
            except ProbeError as exc:
                outcome = "errors"
                logger.warning("probe failed for %s: %s", entry.path, exc)
                self._persist_excluded(entry, f"probe failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the scan
                outcome = "errors"
                logger.exception("scan error for %s", entry.path)
                self._persist_excluded(entry, f"scan error: {type(exc).__name__}: {exc}")

        if lock is not None:
            with lock:
                self._bump(progress, entry, outcome, progress_cb)
        else:
            self._bump(progress, entry, outcome, progress_cb)

    @staticmethod
    def _bump(
        progress: ScanProgress,
        entry: FileEntry,
        outcome: str,
        progress_cb: ProgressFn | None,
    ) -> None:
        progress.current_path = entry.path
        setattr(progress, outcome, getattr(progress, outcome) + 1)
        progress.done += 1
        if progress_cb:
            progress_cb(progress)

    def _process(self, entry: FileEntry) -> None:
        probe = self._probe(entry.path, entry.size_bytes)
        classification = classify(entry.path)
        if self._content is not None:
            content_type, is_anime, source = self._content(entry.path, classification)
            classification.content_type = content_type
            classification.is_anime = is_anime
            classification.content_source = source
        if probe.width == 0 or probe.height == 0 or probe.duration_s <= 0:
            score = ScoreResult(
                bpp_real=0, bpp_target=0, overhead_ratio=0,
                est_out_bytes=entry.size_bytes, est_gain_bytes=0, score=0,
                excluded_reason="unreadable video stream",
            )
        else:
            score = self._score(probe, classification)
        mf = MediaFile(
            id=None,
            path=entry.path,
            size_bytes=entry.size_bytes,
            mtime=entry.mtime,
            probe=probe,
            classification=classification,
            score=score,
        )
        self._repo.upsert(mf, self._now())

    def _persist_excluded(self, entry: FileEntry, reason: str) -> None:
        classification = classify(entry.path)
        score = ScoreResult(
            bpp_real=0, bpp_target=0, overhead_ratio=0,
            est_out_bytes=entry.size_bytes, est_gain_bytes=0, score=0,
            excluded_reason=reason,
        )
        mf = MediaFile(
            id=None, path=entry.path, size_bytes=entry.size_bytes, mtime=entry.mtime,
            probe=None, classification=classification, score=score,
        )
        self._repo.upsert(mf, self._now())
