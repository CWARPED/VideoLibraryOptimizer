"""Domain-specific exceptions."""

from __future__ import annotations


class VLOError(Exception):
    """Base class for all application errors."""


class ProbeError(VLOError):
    """ffprobe failed or returned unusable data for a file."""


class EncodeError(VLOError):
    """The ffmpeg encode process failed."""


class ValidationError(VLOError):
    """A post-encode validation check failed (file is not safe to swap in)."""


class DiskSpaceError(VLOError):
    """Not enough free space to copy/encode."""


class FFmpegNotFoundError(VLOError):
    """ffmpeg or ffprobe binary could not be located."""
