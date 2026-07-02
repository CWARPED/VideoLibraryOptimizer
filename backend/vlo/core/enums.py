"""Enumerations shared across the application."""

from __future__ import annotations

from enum import Enum


class Codec(str, Enum):
    """Target video codec for re-encoding."""

    X265 = "X265"
    SVTAV1 = "SVTAV1"


class MediaKind(str, Enum):
    """Whether a media file is a standalone movie or a series episode."""

    MOVIE = "MOVIE"
    EPISODE = "EPISODE"
    UNKNOWN = "UNKNOWN"


class SubKind(str, Enum):
    """How a subtitle stream should be handled when re-muxing into MKV."""

    COPY = "COPY"  # bitmap (pgs/vobsub) or text (ass/srt) — keep as-is
    TO_SRT = "TO_SRT"  # mov_text from MP4 — transcode to srt to survive in MKV
    DROP = "DROP"  # unsupported, exclude (rare)


class JobState(str, Enum):
    """Lifecycle of a re-encode job."""

    QUEUED = "QUEUED"
    COPYING_IN = "COPYING_IN"
    READY = "READY"  # local copy staged, waiting for a free encode slot (prefetch)
    ENCODING = "ENCODING"
    PAUSED = "PAUSED"  # encode suspended by the user (ffmpeg process frozen)
    VALIDATING = "VALIDATING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    COPYING_BACK = "COPYING_BACK"
    REPLACING = "REPLACING"
    DONE = "DONE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobState.DONE,
            JobState.REJECTED,
            JobState.CANCELLED,
            JobState.FAILED,
        }

    @property
    def is_active_in_worker(self) -> bool:
        """States the worker actively processes (reset to QUEUED on crash recovery)."""
        return self in {
            JobState.COPYING_IN,
            JobState.READY,
            JobState.ENCODING,
            JobState.PAUSED,
            JobState.VALIDATING,
        }
