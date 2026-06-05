"""Download and version helpers for a bundled ffmpeg/ffprobe (stdlib only).

Used both by the portable launcher (first-run download) and the Settings API
(version check + update). Source builds: BtbN FFmpeg-Builds (GPL, ships with
libx265 and libsvtav1). Network/parse failures never raise raw tracebacks to
the caller -- they raise :class:`FfmpegFetchError` with a readable message, and
the read-only inspectors return ``None``.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path

# A stable "latest" GPL win64 build (contains libx265 + libsvtav1).
BTBN_ZIP_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"

_TIMEOUT = 20
_UA = {"User-Agent": "VideoLibraryOptimizer"}
# e.g. "ffmpeg version N-119876-g... " or "...-20260604-..." -> pull a date if present.
_BUILD_DATE_RE = re.compile(r"(20\d{6})")


class FfmpegFetchError(RuntimeError):
    """Raised when downloading/extracting ffmpeg fails (readable message)."""


def _download_url(url: str) -> str | None:
    return BTBN_ZIP_URL if url is None else url


def current_info(ffmpeg_path: str) -> dict | None:
    """Return ``{version, build_date}`` parsed from ``ffmpeg -version`` or None."""
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    if not first.startswith("ffmpeg version"):
        return None
    version = first[len("ffmpeg version"):].strip().split()[0] if first else ""
    m = _BUILD_DATE_RE.search(first)
    return {"version": version, "build_date": m.group(1) if m else None}


def latest_release() -> dict | None:
    """Return ``{published_at, tag}`` for the BtbN 'latest' release, or None offline."""
    try:
        req = urllib.request.Request(RELEASE_API, headers=_UA)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    published = data.get("published_at")  # e.g. "2026-06-04T12:34:56Z"
    return {"published_at": published, "tag": data.get("tag_name")}


def update_available(ffmpeg_path: str) -> bool | None:
    """True if the remote build is newer than the installed one; None if unknown."""
    info = current_info(ffmpeg_path)
    rel = latest_release()
    if not info or not rel or not rel.get("published_at"):
        return None
    local = info.get("build_date")
    if not local:
        return None
    # Compare YYYYMMDD strings: local build date vs remote published date.
    remote = rel["published_at"][:10].replace("-", "")
    if not remote.isdigit():
        return None
    return remote > local


def download_latest(dest_dir: Path | str, *, url: str | None = None, force: bool = False) -> Path:
    """Download the latest ffmpeg build and extract ffmpeg.exe + ffprobe.exe.

    Returns the path to the extracted ``ffmpeg.exe``. Raises FfmpegFetchError on
    any network/extraction problem or if the build lacks the required encoders.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    ffmpeg_exe = dest / "ffmpeg.exe"
    if ffmpeg_exe.exists() and not force:
        return ffmpeg_exe

    src = _download_url(url)
    try:
        req = urllib.request.Request(src, headers=_UA)
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
    except Exception as exc:  # network, DNS, HTTP error
        raise FfmpegFetchError(f"Téléchargement de ffmpeg impossible : {exc}") from exc

    wanted = ("ffmpeg.exe", "ffprobe.exe")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = {
                Path(n).name: n
                for n in zf.namelist()
                if Path(n).name in wanted and "/bin/" in n.replace("\\", "/")
            }
            if "ffmpeg.exe" not in members or "ffprobe.exe" not in members:
                raise FfmpegFetchError("Archive ffmpeg inattendue (binaires introuvables).")
            for name, member in members.items():
                with zf.open(member) as fsrc, open(dest / name, "wb") as fdst:
                    fdst.write(fsrc.read())
    except zipfile.BadZipFile as exc:
        raise FfmpegFetchError("Archive ffmpeg corrompue.") from exc

    if not _has_required_encoders(str(ffmpeg_exe)):
        raise FfmpegFetchError(
            "Le build ffmpeg téléchargé ne contient pas libx265/libsvtav1."
        )
    return ffmpeg_exe


def ensure_ffmpeg(dest_dir: Path | str, *, url: str | None = None) -> Path:
    """Download ffmpeg into ``dest_dir`` if not already present (idempotent)."""
    return download_latest(dest_dir, url=url, force=False)


def _has_required_encoders(ffmpeg_path: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "libx265" in out and "libsvtav1" in out
