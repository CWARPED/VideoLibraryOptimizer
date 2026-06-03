"""Recursive library scanner with mtime/size-based caching.

Walks every subfolder of a root, and for each video file either reuses the
cached probe (when size+mtime are unchanged) or probes/classifies/scores it
afresh. Probe and score functions are injected so the walk logic stays
testable without ffprobe.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
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
    ) -> ScanProgress:
        entries = list(iter_video_files(root))
        total = len(entries)
        progress = ScanProgress(total=total, done=0, current_path="", probed=0, cached=0, errors=0)
        present: set[str] = set()

        for entry in entries:
            if should_cancel and should_cancel():
                break
            present.add(entry.path)
            progress.current_path = entry.path

            if not force and self._repo.cache_is_fresh(entry.path, entry.size_bytes, entry.mtime):
                progress.cached += 1
            else:
                try:
                    self._process(entry)
                    progress.probed += 1
                except ProbeError as exc:
                    progress.errors += 1
                    logger.warning("probe failed for %s: %s", entry.path, exc)
                    self._persist_excluded(entry, f"probe failed: {exc}")
                except Exception as exc:  # noqa: BLE001 - one bad file must not kill the scan
                    progress.errors += 1
                    logger.exception("scan error for %s", entry.path)
                    self._persist_excluded(entry, f"scan error: {type(exc).__name__}: {exc}")

            progress.done += 1
            if progress_cb:
                progress_cb(progress)

        # Drop cache rows for files that no longer exist (only on a full scan).
        # A failure here must never lose an otherwise-completed scan.
        if not (should_cancel and should_cancel()):
            try:
                self._repo.delete_missing(present)
            except Exception:  # noqa: BLE001
                logger.exception("delete_missing failed (scan results kept)")
        return progress

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
