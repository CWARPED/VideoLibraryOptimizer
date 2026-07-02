"""Scan endpoints (several scans can run in parallel, on different roots)."""

from __future__ import annotations

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
    try:
        # Fire-and-forget; progress is reported over WebSocket and /scan/status.
        session = state.start_scan(req.root_path, req.force)
    except RuntimeError as exc:  # same root already scanning
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, "scan_id": session.id, "root_path": req.root_path}


@router.get("/scan/status")
async def scan_status(request: Request):
    state = get_state(request)
    return {"scans": [s.to_dict() for s in state.scans.values()]}


@router.post("/scan/cancel/{scan_id}")
async def cancel_scan(scan_id: str, request: Request):
    if not get_state(request).cancel_scan(scan_id):
        raise HTTPException(status_code=404, detail=f"unknown scan: {scan_id}")
    return {"cancelling": True, "scan_id": scan_id}


@router.post("/scan/cancel")
async def cancel_all_scans(request: Request):
    get_state(request).cancel_scan()
    return {"cancelling": True}
