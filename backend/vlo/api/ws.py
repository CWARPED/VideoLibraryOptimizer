"""WebSocket endpoint streaming progress and queue updates."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .common import get_state_ws
from .schemas import job_to_dict

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    state = get_state_ws(websocket)
    bus = state.broadcaster
    queue = bus.subscribe()

    # Initial snapshot so a fresh client is in sync before deltas arrive.
    await websocket.send_json({
        "type": "snapshot",
        "jobs": [job_to_dict(j) for j in state.jobs_repo.list()],
        "scan": {
            "running": state.scan_status.running,
            "done": state.scan_status.done,
            "total": state.scan_status.total,
        },
    })

    async def receiver() -> None:
        # Drain inbound messages (and detect disconnect) — we don't expect any.
        while True:
            await websocket.receive_text()

    recv_task = asyncio.create_task(receiver())
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        recv_task.cancel()
        bus.unsubscribe(queue)
