"""Settings and encode-profile endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .. import ffmpeg_dist
from ..core.models import EncodeProfile
from ..metadata.keywords import DEFAULT_ANIMATION_KEYWORDS
from .common import get_state
from .schemas import ProfileUpdate, SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])

_SCORING_KEYS = (
    "weight_overhead", "weight_gain", "gain_ref_gb",
    "min_overhead_ratio", "exclude_dolby_vision",
)
_KV_KEYS = (
    "tmdb_api_key", "tmdb_enabled", "animation_keywords",
    "max_parallel_encodes", "filename_tag", "rewrite_codec_tags",
    "audio_lossless_to_opus", "av1_8bit",
)


def _bands_json(repo, content_type: str) -> list[dict]:
    return [
        {"height_min": a, "height_max": b, "bpp_target": c}
        for a, b, c in repo.reference_bands(content_type)
    ]


@router.get("/settings")
async def get_settings_endpoint(request: Request):
    state = get_state(request)
    repo = state.settings_repo
    s = state.settings
    return {
        "scoring": {
            "weight_overhead": repo.get("weight_overhead", s.weight_overhead),
            "weight_gain": repo.get("weight_gain", s.weight_gain),
            "gain_ref_gb": repo.get("gain_ref_gb", s.gain_ref_gb),
            "min_overhead_ratio": repo.get("min_overhead_ratio", s.min_overhead_ratio),
            "exclude_dolby_vision": repo.get("exclude_dolby_vision", s.exclude_dolby_vision),
        },
        "reference_bands": _bands_json(repo, "live_action"),
        "animation_bands": _bands_json(repo, "animation"),
        "content_detection": {
            "tmdb_api_key": repo.get("tmdb_api_key", s.tmdb_api_key),
            "tmdb_enabled": repo.get("tmdb_enabled", s.tmdb_enabled),
            "animation_keywords": repo.get("animation_keywords", DEFAULT_ANIMATION_KEYWORDS),
        },
        "encoding": {
            "max_parallel_encodes": repo.get("max_parallel_encodes", s.max_parallel_encodes),
            "filename_tag": repo.get("filename_tag", s.filename_tag),
            "rewrite_codec_tags": repo.get("rewrite_codec_tags", s.rewrite_codec_tags),
            "audio_lossless_to_opus": repo.get(
                "audio_lossless_to_opus", s.audio_lossless_to_opus
            ),
            "av1_8bit": repo.get("av1_8bit", s.av1_8bit),
        },
        "work_dir": repo.get("work_dir", str(s.work_dir)),
        "duration_tolerance_pct": s.duration_tolerance_pct,
    }


@router.put("/settings")
async def update_settings(update: SettingsUpdate, request: Request):
    repo = get_state(request).settings_repo
    data = update.model_dump(exclude_none=True)
    for key in (*_SCORING_KEYS, *_KV_KEYS):
        if key in data:
            repo.set(key, data[key])
    if "work_dir" in data:
        wd = Path(data["work_dir"]).expanduser()
        try:
            wd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"répertoire de travail invalide: {exc}"
            ) from exc
        repo.set("work_dir", str(wd))
    if "reference_bands" in data:
        repo.replace_reference_bands(
            [(int(a), int(b), float(c)) for a, b, c in data["reference_bands"]], "live_action"
        )
    if "animation_bands" in data:
        repo.replace_reference_bands(
            [(int(a), int(b), float(c)) for a, b, c in data["animation_bands"]], "animation"
        )
    return {"ok": True, "updated": list(data.keys())}


@router.get("/profiles")
async def list_profiles(request: Request):
    repo = get_state(request).settings_repo
    return {
        "profiles": [
            {
                "name": p.name,
                "crf_x265": p.crf_x265, "crf_av1": p.crf_av1,
                "preset_x265": p.preset_x265, "preset_av1": p.preset_av1,
                "floor_x265": p.floor_x265, "floor_av1": p.floor_av1,
                "x265_params": p.x265_params, "svtav1_params": p.svtav1_params,
            }
            for p in repo.list_profiles()
        ]
    }


@router.put("/profiles/{name}")
async def update_profile(name: str, update: ProfileUpdate, request: Request):
    repo = get_state(request).settings_repo
    if repo.get_profile(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown profile: {name}")
    repo.upsert_profile(EncodeProfile(name=name, **update.model_dump()))
    return {"ok": True}


def _ffmpeg_info_blocking(ffmpeg: str) -> dict:
    """Gather ffmpeg version + remote release info (blocking subprocess/network)."""
    info = ffmpeg_dist.current_info(ffmpeg)
    rel = ffmpeg_dist.latest_release()
    update = None
    if info and info.get("build_date") and rel and rel.get("published_at"):
        remote = rel["published_at"][:10].replace("-", "")
        if remote.isdigit():
            update = remote > info["build_date"]
    return {
        "path": ffmpeg,
        "bundled": Path(ffmpeg).is_file() and Path(ffmpeg).is_absolute(),
        "version": info["version"] if info else None,
        "build_date": info["build_date"] if info else None,
        "latest_published_at": rel["published_at"] if rel else None,
        "update_available": update,
    }


@router.get("/ffmpeg")
async def ffmpeg_info(request: Request):
    """Current ffmpeg version + whether a newer build is available."""
    ffmpeg, _ = get_state(request).settings.resolve_binaries()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ffmpeg_info_blocking, ffmpeg)


@router.post("/ffmpeg/update")
async def ffmpeg_update(request: Request):
    """Download the latest ffmpeg build (refused while an encode is running)."""
    state = get_state(request)
    if state.job_manager.has_active():
        raise HTTPException(
            status_code=409,
            detail="Un encodage est en cours ; impossible de mettre à jour ffmpeg maintenant.",
        )
    ffmpeg, _ = state.settings.resolve_binaries()
    dest = Path(ffmpeg).parent if Path(ffmpeg).is_absolute() else Path("ffmpeg/bin")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: ffmpeg_dist.download_latest(dest, force=True)
        )
    except ffmpeg_dist.FfmpegFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    info = ffmpeg_dist.current_info(str(dest / "ffmpeg.exe"))
    return {"ok": True, "version": info["version"] if info else None}
