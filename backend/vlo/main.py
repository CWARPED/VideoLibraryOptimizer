"""FastAPI application: wiring, lifespan and static frontend."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import sysstats
from .api import (
    routes_jobs,
    routes_library,
    routes_logs,
    routes_scan,
    routes_settings,
    routes_system,
    ws,
)
from .config import get_settings
from .deps import AppState, build_app_state

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: AppState | None = getattr(app.state, "app", None)
    if state is None:
        state = build_app_state()
        app.state.app = state
    await state.job_manager.start()
    sys_task = asyncio.create_task(sysstats.broadcast_loop(state.broadcaster))
    try:
        yield
    finally:
        sys_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sys_task
        await state.job_manager.stop()
        state.db.close()


def create_app(state: AppState | None = None) -> FastAPI:
    app = FastAPI(title="VideoLibraryOptimizer", lifespan=lifespan)
    if state is not None:
        app.state.app = state
    app.include_router(routes_scan.router)
    app.include_router(routes_library.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_logs.router)
    app.include_router(routes_system.router)
    app.include_router(ws.router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    if _FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()


def run() -> None:
    """Entry point for the ``vlo`` console script."""
    import uvicorn

    s = get_settings()
    uvicorn.run("vlo.main:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    run()
