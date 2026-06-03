"""Convenience probe service: stat + ffprobe + parse into a ProbeResult."""

from __future__ import annotations

import os

from ..core.models import ProbeResult
from .ffprobe import run_ffprobe
from .parser import parse_probe


class ProbeService:
    def __init__(self, ffprobe_bin: str) -> None:
        self._ffprobe = ffprobe_bin

    def probe(self, path: str, size: int | None = None) -> ProbeResult:
        if size is None:
            size = os.path.getsize(path)
        data = run_ffprobe(self._ffprobe, path)
        return parse_probe(data, path, size)
