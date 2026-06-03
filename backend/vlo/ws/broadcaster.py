"""In-process pub/sub fan-out for WebSocket clients."""

from __future__ import annotations

import asyncio
from typing import Any


class Broadcaster:
    """Fan messages out to every subscribed WebSocket client.

    Each subscriber gets a bounded queue; if a slow client's queue fills up,
    its oldest message is dropped rather than blocking the publisher.
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - race with concurrent reader
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
