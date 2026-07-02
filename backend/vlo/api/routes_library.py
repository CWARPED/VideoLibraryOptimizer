"""Library (movies / series) endpoints.

When ``codec`` and ``profile`` query params are supplied, the estimated output
size, gain and priority score are recomputed on the fly using that profile's
per-codec floor ratio — so the numbers reflect the encode the user is about to
launch. Without them, the cached default estimate is returned.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, Request

import time

from ..core.enums import Codec, MediaKind
from ..core.models import MediaFile
from ..scoring.score import compute_score
from .common import get_state
from .schemas import ContentTypeUpdate, exclusion_category, media_to_dict

router = APIRouter(prefix="/api", tags=["library"])


def _is_candidate(media_dict: dict) -> bool:
    """A file proposed for re-encode: not excluded and not already re-encoded."""
    return media_dict.get("excluded_reason") is None and not media_dict.get("reencoded")


def _parse_codec(value: str | None) -> Codec | None:
    if value is None:
        return None
    try:
        return Codec(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid codec: {value}") from exc


def _rescore(state, media: list[MediaFile], codec: Codec | None, profile: str | None) -> None:
    """Recompute each item's score in place for the chosen codec+profile."""
    if codec is None or not profile:
        return
    cfg = state.scoring_config_for(codec, profile)
    for mf in media:
        if mf.probe is not None:
            mf.score = compute_score(mf.probe, mf.classification, cfg)


@router.get("/movies")
async def list_movies(
    request: Request,
    only_candidates: bool = Query(True),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    codec: str | None = Query(None),
    profile: str | None = Query(None),
):
    state = get_state(request)
    cd = _parse_codec(codec)

    if cd and profile:
        # Recompute against the chosen codec/profile, then filter + sort here.
        movies = state.scan_repo.list_movies(only_candidates=False, limit=5000, offset=0)
        _rescore(state, movies, cd, profile)
        if only_candidates:
            movies = [m for m in movies if m.score and m.score.excluded_reason is None
                      and m.reencoded_at is None]
        movies.sort(key=lambda m: (m.score.score if m.score else 0), reverse=True)
        movies = movies[offset:offset + limit]
    else:
        movies = state.scan_repo.list_movies(
            only_candidates=only_candidates, limit=limit, offset=offset
        )
    return {"movies": [media_to_dict(m) for m in movies]}


@router.get("/excluded")
async def list_excluded(request: Request):
    """Files skipped during scan, with their reason and a UI category."""
    media = get_state(request).scan_repo.list_excluded()
    out = []
    for mf in media:
        if mf.reencoded_at is not None:
            reason, category = "déjà réencodé par l'application", "reencoded"
        else:
            reason = mf.score.excluded_reason if mf.score else None
            category = exclusion_category(reason)
        out.append({
            "filename": media_to_dict(mf)["filename"],
            "path": mf.path,
            "reason": reason,
            "category": category,
        })
    return {"excluded": out}


@router.get("/series")
async def list_series(
    request: Request,
    codec: str | None = Query(None),
    profile: str | None = Query(None),
):
    state = get_state(request)
    cd = _parse_codec(codec)
    if not (cd and profile):
        return {"series": state.scan_repo.list_series_summary()}

    # Codec/profile-aware aggregation, computed in Python from all episodes.
    episodes = state.scan_repo.list_all_episodes()
    _rescore(state, episodes, cd, profile)
    by_slug: dict[str, list[MediaFile]] = defaultdict(list)
    for ep in episodes:
        slug = ep.classification.series_slug if ep.classification else None
        if slug:
            by_slug[slug].append(ep)

    summary = []
    for slug, eps in by_slug.items():
        candidates = [e for e in eps if e.score and e.score.excluded_reason is None]
        title = next((e.classification.series_title for e in eps if e.classification), slug)
        is_animation = any(
            e.classification and e.classification.content_type == "animation" for e in eps
        )
        is_anime = any(e.classification and e.classification.is_anime for e in eps)
        summary.append({
            "series_slug": slug,
            "series_title": title,
            "n_episodes": len(eps),
            "n_candidates": len(candidates),
            "est_gain_bytes": sum(e.score.est_gain_bytes for e in candidates),
            "top_score": max((e.score.score for e in eps if e.score), default=0),
            "content_type": "animation" if is_animation else "live_action",
            "is_anime": is_anime,
        })
    summary.sort(key=lambda s: s["est_gain_bytes"], reverse=True)
    return {"series": summary}


@router.delete("/movies")
async def clear_movies(request: Request):
    """Clear the cached movie rows (files on disk are untouched; a rescan repopulates)."""
    removed = get_state(request).scan_repo.delete_by_kind(MediaKind.MOVIE)
    return {"ok": True, "removed": removed}


@router.delete("/series")
async def clear_series(request: Request):
    """Clear the cached series/episode rows (files on disk are untouched)."""
    removed = get_state(request).scan_repo.delete_by_kind(MediaKind.EPISODE)
    return {"ok": True, "removed": removed}


@router.post("/media/{file_id}/content_type")
async def set_content_type(file_id: int, update: ContentTypeUpdate, request: Request):
    """Manually override a file's content type (locked from re-scan) + rescore."""
    state = get_state(request)
    repo = state.scan_repo
    mf = repo.get_by_id(file_id)
    if mf is None:
        raise HTTPException(status_code=404, detail="media not found")
    ct = update.content_type
    if ct not in ("animation", "live_action"):
        raise HTTPException(status_code=400, detail="invalid content_type")
    is_anime = update.is_anime and ct == "animation"

    repo.set_content_type(file_id, ct, is_anime=is_anime, source="manual")
    # Recompute the score against the new content type.
    mf = repo.get_by_id(file_id)
    if mf and mf.probe is not None:
        mf.score = compute_score(mf.probe, mf.classification, state.scoring_config())
        repo.upsert(mf, time.time())
    return media_to_dict(repo.get_by_id(file_id))


@router.get("/series/{slug}")
async def get_series(
    slug: str,
    request: Request,
    codec: str | None = Query(None),
    profile: str | None = Query(None),
):
    state = get_state(request)
    episodes = state.scan_repo.list_episodes(slug)
    if not episodes:
        raise HTTPException(status_code=404, detail=f"no series '{slug}'")
    _rescore(state, episodes, _parse_codec(codec), profile)

    seasons: dict[int, list] = {}
    for ep in episodes:
        season = ep.classification.season if ep.classification else None
        seasons.setdefault(season if season is not None else -1, []).append(media_to_dict(ep))

    series_title = next(
        (e.classification.series_title for e in episodes if e.classification), slug
    )
    return {
        "slug": slug,
        "series_title": series_title,
        "seasons": [
            {
                "season": season if season != -1 else None,
                "episodes": eps,
                "n_candidates": sum(1 for e in eps if _is_candidate(e)),
                "est_gain_bytes": sum(e["est_gain_bytes"] or 0 for e in eps if _is_candidate(e)),
            }
            for season, eps in sorted(seasons.items())
        ],
    }
