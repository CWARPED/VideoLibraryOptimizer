"""Thin wrapper around the ffprobe binary."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from ..core.errors import ProbeError


def run_ffprobe(ffprobe_bin: str, path: str, *, timeout: float = 120.0) -> dict[str, Any]:
    """Run ffprobe and return the parsed JSON (format + streams + chapters).

    Raises ProbeError on a non-zero exit or unparseable output.
    """
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # binary missing
        raise ProbeError(f"ffprobe binary not found: {ffprobe_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out for {path}") from exc

    if proc.returncode != 0:
        raise ProbeError(
            f"ffprobe failed for {path} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
