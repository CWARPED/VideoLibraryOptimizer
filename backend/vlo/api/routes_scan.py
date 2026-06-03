"""Scan endpoints."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException, Request

from .common import get_state
from .schemas import ScanRequest

router = APIRouter(prefix="/api", tags=["scan"])


@router.post("/scan")
async def start_scan(req: ScanRequest, request: Request):
    state = get_state(request)
    if not os.path.isdir(req.root_path):
        raise HTTPException(status_code=400, detail=f"not a directory: {req.root_path}")
    if state.scan_status.running:
        raise HTTPException(status_code=409, detail="a scan is already running")

    # Fire-and-forget; progress is reported over WebSocket and /scan/status.
    asyncio.create_task(_run(state, req.root_path, req.force))
    return {"started": True, "root_path": req.root_path}


async def _run(state, root: str, force: bool) -> None:
    try:
        await state.run_scan(root, force=force)
    except Exception:  # noqa: BLE001 - error is recorded in scan_status
        pass


@router.get("/scan/status")
async def scan_status(request: Request):
    s = get_state(request).scan_status
    return {
        "running": s.running,
        "root": s.root,
        "total": s.total,
        "done": s.done,
        "probed": s.probed,
        "cached": s.cached,
        "errors": s.errors,
        "current_path": s.current_path,
        "last_error": s.last_error,
    }


@router.post("/scan/cancel")
async def cancel_scan(request: Request):
    get_state(request).cancel_scan()
    return {"cancelling": True}
