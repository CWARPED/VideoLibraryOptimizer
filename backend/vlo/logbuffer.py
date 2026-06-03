"""In-memory ring buffer of recent log records, exposed to the UI."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any


class RingLogHandler(logging.Handler):
    """Keeps the last ``capacity`` log records as plain dicts."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:  # pragma: no cover - never let logging crash the app
            message = str(record.msg)
        self._seq += 1
        self._records.append({
            "seq": self._seq,
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        })

    def records(self, *, level: str | None = None, since: int = 0) -> list[dict[str, Any]]:
        out = [r for r in self._records if r["seq"] > since]
        if level:
            wanted = _level_and_above(level)
            out = [r for r in out if r["level"] in wanted]
        return out

    def clear(self) -> None:
        self._records.clear()


_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _level_and_above(level: str) -> set[str]:
    level = level.upper()
    if level not in _LEVELS:
        return set(_LEVELS)
    return set(_LEVELS[_LEVELS.index(level):])


# Module-level singleton, attached to the "vlo" logger tree at startup.
LOG_BUFFER = RingLogHandler()


def setup_logging(level: int = logging.INFO) -> RingLogHandler:
    """Attach the ring buffer (and a stderr handler) to the 'vlo' logger once."""
    LOG_BUFFER.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    vlo_logger = logging.getLogger("vlo")
    vlo_logger.setLevel(level)
    if not any(isinstance(h, RingLogHandler) for h in vlo_logger.handlers):
        vlo_logger.addHandler(LOG_BUFFER)
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        vlo_logger.addHandler(stream)
    vlo_logger.propagate = False
    return LOG_BUFFER
