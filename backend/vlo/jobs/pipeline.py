"""File-movement steps of a job: copy in, decode-check, safe replace.

The original file on the (possibly NAS) source is only ever touched in the
final replace step, and even then via an atomic ``os.replace`` of a fully
written temp file — so a crash or network drop never leaves a partial file in
place of the original.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path


def _long(path: Path) -> str:
    """Windows long-path-safe string for shutil/os operations."""
    s = str(path)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        abs = os.path.abspath(s)
        if abs.__len__() >= 240:
            if abs.startswith("\\\\"):  # UNC \\server\share -> \\?\UNC\server\share
                return "\\\\?\\UNC\\" + abs[2:]
            return "\\\\?\\" + abs
    return s


def copy_into(src: Path, dst_dir: Path) -> Path:
    """Copy ``src`` into ``dst_dir`` keeping its name; return the new path."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(_long(src), _long(dst))
    return dst


def final_output_path(original_path: Path, final_stem: str | None = None) -> Path:
    """Where the re-encoded file belongs in the original folder (always .mkv).

    ``final_stem`` overrides the filename stem (for an appended tag / rewritten
    codec token); otherwise the original name is kept.
    """
    if final_stem:
        return original_path.with_name(f"{final_stem}.mkv")
    return original_path.with_suffix(".mkv")


def safe_replace(local_out: Path, original_path: Path, final_stem: str | None = None) -> Path:
    """Place ``local_out`` into the original folder, replacing the original.

    Returns the final path. The output is always MKV; if the original had a
    different name/extension it is removed after the new file is in place.
    """
    final = final_output_path(original_path, final_stem)
    tmp = final.with_name(final.stem + ".vlo-tmp.mkv")

    shutil.copy2(_long(local_out), _long(tmp))
    if os.path.getsize(_long(tmp)) != os.path.getsize(_long(local_out)):
        os.remove(_long(tmp))
        raise OSError("size mismatch after copying output to destination")

    os.replace(_long(tmp), _long(final))  # atomic; overwrites a same-named .mkv

    # If the source had a different container (e.g. .mp4), drop the old file.
    if original_path != final and original_path.exists():
        os.remove(_long(original_path))

    return final


async def decode_check(ffmpeg_bin: str, path: Path, *, timeout: float = 1800.0) -> bool:
    """Decode the output's video and audio to a null muxer to detect corruption.

    Runs synchronously in a worker thread (not via asyncio subprocess) so it
    works under any event loop, including the SelectorEventLoop used by uvicorn
    in --reload mode on Windows. Subtitles are intentionally not mapped: they
    are stream-copied (never re-encoded) and the null muxer cannot encode them,
    which would otherwise raise a spurious error.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _decode_check_blocking, ffmpeg_bin, str(path), timeout)


def _decode_check_blocking(ffmpeg_bin: str, path: str, timeout: float) -> bool:
    args = [
        ffmpeg_bin, "-v", "error", "-hide_banner",
        "-i", path, "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and not proc.stderr.strip()
