"""Job queue endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..core.enums import JobState
from .common import get_state
from .schemas import BatchRequest, job_to_dict

router = APIRouter(prefix="/api", tags=["jobs"])


@router.post("/jobs/batch")
async def create_batch(req: BatchRequest, request: Request):
    state = get_state(request)
    repo = state.scan_repo

    media = []
    if req.file_ids:
        for fid in req.file_ids:
            mf = repo.get_by_id(fid)
            if mf is not None:
                media.append(mf)
    elif req.series_slug is not None:
        if req.season is not None:
            media = repo.list_season_episodes(req.series_slug, req.season)
        else:
            media = repo.list_episodes(req.series_slug)

    # Only enqueue actual candidates: not excluded and not already re-encoded.
    media = [
        m for m in media
        if (m.score is None or m.score.excluded_reason is None) and m.reencoded_at is None
    ]
    if not media:
        raise HTTPException(status_code=400, detail="no eligible files in selection")

    try:
        batch_id = state.job_manager.enqueue(media, req.codec, req.profile_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"batch_id": batch_id, "count": len(media)}


@router.get("/jobs")
async def list_jobs(
    request: Request,
    state_filter: str | None = Query(None, alias="state"),
    batch_id: str | None = Query(None),
):
    repo = get_state(request).jobs_repo
    js: JobState | None = None
    if state_filter:
        try:
            js = JobState(state_filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid state") from exc
    jobs = repo.list(state=js, batch_id=batch_id)
    return {"jobs": [job_to_dict(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, request: Request):
    job = get_state(request).jobs_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job_to_dict(job)


@router.post("/jobs/{job_id}/confirm")
async def confirm_job(job_id: int, request: Request):
    state = get_state(request)
    mgr = state.job_manager
    try:
        job = await mgr.confirm(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Refresh the cache for the now re-encoded file (re-probe + re-score) so it
    # no longer shows up as a heavy candidate.
    if job.media_file_id is not None:
        await state.refresh_media_file(job.media_file_id)
    return job_to_dict(job)


@router.post("/jobs/{job_id}/reject")
async def reject_job(job_id: int, request: Request):
    mgr = get_state(request).job_manager
    try:
        mgr.reject(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request):
    get_state(request).job_manager.cancel(job_id)
    return {"ok": True}


@router.get("/stats")
async def get_stats(request: Request):
    """Cumulative space saved across all completed re-encodes (persistent)."""
    repo = get_state(request).settings_repo
    return {
        "total_gain_bytes": repo.get("total_gain_bytes", 0) or 0,
        "total_encodes_done": repo.get("total_encodes_done", 0) or 0,
    }


@router.post("/jobs/clear")
async def clear_jobs(request: Request):
    """Remove all terminal jobs (done/failed/rejected/cancelled) from the queue."""
    removed = get_state(request).jobs_repo.delete_terminal()
    return {"ok": True, "removed": removed}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: int, request: Request):
    """Delete a single terminal job. Active jobs must be cancelled first."""
    repo = get_state(request).jobs_repo
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not repo.delete(job_id):
        raise HTTPException(status_code=409, detail="job is active; cancel it first")
    return {"ok": True}
