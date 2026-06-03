"""Free-space checks for the work directory and destination."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..core.errors import DiskSpaceError


def free_bytes(path: Path) -> int:
    """Free bytes on the volume containing ``path`` (nearest existing parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def ensure_space(path: Path, required_bytes: int, *, margin_bytes: int = 0) -> None:
    """Raise DiskSpaceError unless ``path``'s volume has the required free space."""
    available = free_bytes(path)
    needed = required_bytes + margin_bytes
    if available < needed:
        raise DiskSpaceError(
            f"Not enough space on {path}: need {needed} bytes, have {available}"
        )
