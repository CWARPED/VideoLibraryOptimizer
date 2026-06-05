"""Application configuration (environment + defaults)."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Video container extensions we consider part of a library.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm"}
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings, overridable via VLO_* environment variables or .env."""

    model_config = SettingsConfigDict(env_prefix="VLO_", env_file=".env", extra="ignore")

    db_path: Path = Field(default=_PROJECT_ROOT / "data" / "vlo.db")
    work_dir: Path = Field(default=_PROJECT_ROOT / "work")

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # Scoring weights / references (also persisted in DB settings; env is the boot default).
    weight_overhead: float = 0.5
    weight_gain: float = 0.5
    gain_ref_gb: float = 10.0
    min_overhead_ratio: float = 1.1  # below this, a file is not worth re-encoding

    # Content-type detection (TMDB). Key/enabled also overridable via DB settings.
    tmdb_api_key: str = ""
    tmdb_enabled: bool = True

    # Safety / behaviour.
    exclude_dolby_vision: bool = True
    disk_space_margin_bytes: int = 5 * 1024**3  # 5 GiB headroom in work_dir
    duration_tolerance_pct: float = 0.5  # |out-src| must be within this % of source duration

    # Encoding throughput / output naming.
    max_parallel_encodes: int = 2  # one 1080p encode under-uses a 16c/32t CPU
    filename_tag: str = ""  # appended to the output filename stem, e.g. " x265"
    rewrite_codec_tags: bool = False  # replace codec tokens in name/title (x264->x265…)

    # Server.
    host: str = "127.0.0.1"
    port: int = 8077

    def resolve_binaries(self) -> tuple[str, str]:
        """Return (ffmpeg, ffprobe) absolute paths, resolving via PATH if needed."""
        ffmpeg = shutil.which(self.ffmpeg_path) or self.ffmpeg_path
        ffprobe = shutil.which(self.ffprobe_path) or self.ffprobe_path
        return ffmpeg, ffprobe


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
