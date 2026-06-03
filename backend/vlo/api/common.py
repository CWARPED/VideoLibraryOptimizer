"""Shared helpers for API routes."""

from __future__ import annotations

from fastapi import Request, WebSocket

from ..deps import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.app


def get_state_ws(websocket: WebSocket) -> AppState:
    return websocket.app.state.app
