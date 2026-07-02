"""System monitoring endpoint (CPU %, memory, best-effort temperature)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from .. import sysstats

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system")
async def system_stats():
    """Current CPU load, memory and (best-effort) CPU temperature."""
    return await asyncio.to_thread(sysstats.sample)
