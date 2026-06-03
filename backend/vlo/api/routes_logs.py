"""Log inspection endpoints (backed by the in-memory ring buffer)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..logbuffer import LOG_BUFFER

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
async def get_logs(
    level: str | None = Query(None, description="minimum level: INFO/WARNING/ERROR"),
    since: int = Query(0, description="return records with seq greater than this"),
    limit: int = Query(500, ge=1, le=2000),
):
    records = LOG_BUFFER.records(level=level, since=since)
    return {"logs": records[-limit:]}


@router.post("/logs/clear")
async def clear_logs():
    LOG_BUFFER.clear()
    return {"ok": True}
