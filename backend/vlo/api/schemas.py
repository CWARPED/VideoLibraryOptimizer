"""API request models and serialisers for domain objects."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from ..core.enums import Codec
from ..core.models import Job, MediaFile


# --- requests -----------------------------------------------------------
class ScanRequest(BaseModel):
    root_path: str
    force: bool = False


class BatchRequest(BaseModel):
    codec: Codec
    profile_name: str
    eight_bit: bool = False  # encode 8-bit instead of the default 10-bit (x265 or AV1)
    file_ids: list[int] = Field(default_factory=list)
    series_slug: str | None = None
    season: int | None = None


class ProfileUpdate(BaseModel):
    crf_x265: int
    crf_av1: int
    preset_x265: str
    preset_av1: int
    floor_x265: float
    floor_av1: float
    x265_params: str = ""
    svtav1_params: str = ""


class ContentTypeUpdate(BaseModel):
    content_type: str  # "animation" | "live_action"
    is_anime: bool = False


class SettingsUpdate(BaseModel):
    weight_overhead: float | None = None
    weight_gain: float | None = None
    gain_ref_gb: float | None = None
    min_overhead_ratio: float | None = None
    exclude_dolby_vision: bool | None = None
    work_dir: str | None = None
    # Content-type detection
    tmdb_api_key: str | None = None
    tmdb_enabled: bool | None = None
    animation_keywords: list[str] | None = None
    # bpp reference tables: list of [height_min, height_max, bpp_target]
    reference_bands: list[list[float]] | None = None
    animation_bands: list[list[float]] | None = None
    # Encoding throughput / output naming
    max_parallel_encodes: int | None = None
    filename_tag: str | None = None
    rewrite_codec_tags: bool | None = None
    audio_lossless_to_opus: bool | None = None
    scan_workers: int | None = Field(default=None, ge=1, le=32)


# --- exclusion categorisation ------------------------------------------
def exclusion_category(reason: str | None) -> str:
    """Classify an excluded_reason into a UI category."""
    if not reason:
        return "other"
    r = reason.lower()
    if (
        r.startswith("probe failed")
        or r.startswith("scan error")
        or r.startswith("unreadable")
        or r.startswith("unknown bitrate")
    ):
        return "unreadable"
    if r.startswith("dolby vision"):
        return "dolby_vision"
    if r.startswith("already efficient") or r.startswith("no estimated gain"):
        return "efficient"
    return "other"


# --- serialisers --------------------------------------------------------
def media_to_dict(mf: MediaFile) -> dict[str, Any]:
    p = mf.probe
    c = mf.classification
    s = mf.score
    return {
        "id": mf.id,
        "path": mf.path,
        "filename": os.path.basename(mf.path),
        "size_bytes": mf.size_bytes,
        "kind": c.kind.value if c else "UNKNOWN",
        "title": c.title if c else None,
        "year": c.year if c else None,
        "series_title": c.series_title if c else None,
        "season": c.season if c else None,
        "episode": c.episode if c else None,
        "content_type": c.content_type if c else "live_action",
        "is_anime": c.is_anime if c else False,
        "content_source": c.content_source if c else None,
        "reencoded": mf.reencoded_at is not None,
        "duration_s": p.duration_s if p else None,
        "width": p.width if p else None,
        "height": p.height if p else None,
        "fps": round(p.fps, 3) if p else None,
        "vcodec": p.vcodec if p else None,
        "is_hdr": p.is_hdr if p else None,
        "is_dolby_vision": p.is_dolby_vision if p else None,
        "video_bitrate_bps": p.video_bitrate_bps if p else None,
        "n_audio": p.n_audio if p else None,
        "n_subs": p.n_subs if p else None,
        "overhead_ratio": round(s.overhead_ratio, 2) if s else None,
        "est_out_bytes": s.est_out_bytes if s else None,
        "est_gain_bytes": s.est_gain_bytes if s else None,
        "score": s.score if s else None,
        "excluded_reason": s.excluded_reason if s else None,
    }


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "media_file_id": job.media_file_id,
        "source_path": job.source_path,
        "filename": os.path.basename(job.source_path),
        "codec": job.codec.value,
        "profile_name": job.profile_name,
        "crf": job.crf,
        "preset": job.preset,
        "eight_bit": job.eight_bit,
        "state": job.state.value,
        "progress": round(job.progress, 4),
        "speed": job.speed,
        "eta_s": job.eta_s,
        "batch_id": job.batch_id,
        "size_src_bytes": job.size_src_bytes,
        "size_out_bytes": job.size_out_bytes,
        "gain_bytes": job.gain_bytes,
        "validation_json": job.validation_json,
        "error_message": job.error_message,
    }
