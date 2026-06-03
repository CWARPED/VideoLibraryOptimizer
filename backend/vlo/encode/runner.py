"""Run ffmpeg and parse its -progress stream.

The ffmpeg process is driven from a worker thread using a *synchronous*
``subprocess.Popen`` rather than ``asyncio.create_subprocess_exec``. This is
deliberate: on Windows the SelectorEventLoop (which uvicorn uses in --reload
mode) does not support asyncio subprocesses and raises NotImplementedError.
Running ffmpeg in a thread works under any event loop. Progress callbacks are
marshalled back onto the loop thread so consumers (DB, WebSocket broadcaster)
stay single-threaded.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..core.errors import EncodeError


@dataclass(slots=True)
class EncodeProgress:
    progress: float  # 0..1
    out_time_s: float
    speed: float | None  # e.g. 1.9 (from "1.9x")
    eta_s: float | None


@dataclass(slots=True)
class EncodeResult:
    cancelled: bool
    returncode: int


def _parse_speed(value: str) -> float | None:
    value = value.strip().rstrip("x")
    try:
        return float(value)
    except ValueError:
        return None


class EncodeRunner:
    """Executes one ffmpeg command, streaming progress via a callback."""

    def __init__(self, *, stderr_tail_lines: int = 60) -> None:
        self._stderr_tail = stderr_tail_lines

    async def run(
        self,
        args: list[str],
        *,
        duration_s: float,
        on_progress: Callable[[EncodeProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> EncodeResult:
        """Run ffmpeg in a worker thread. Raises EncodeError on failure.

        Cancellation: if ``cancel_event`` (a threading.Event) is set, the
        process is terminated and EncodeResult(cancelled=True) is returned.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._run_blocking, args, duration_s, on_progress, cancel_event, loop
        )

    def _run_blocking(
        self,
        args: list[str],
        duration_s: float,
        on_progress: Callable[[EncodeProgress], None] | None,
        cancel_event: threading.Event | None,
        loop: asyncio.AbstractEventLoop,
    ) -> EncodeResult:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        stderr_lines: list[str] = []

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_lines.append(line.rstrip())
                if len(stderr_lines) > self._stderr_tail:
                    del stderr_lines[0]

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        current: dict[str, str] = {}
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            current[key] = value
            if key == "progress":
                if on_progress is not None:
                    progress = self._make_progress(current, duration_s)
                    loop.call_soon_threadsafe(on_progress, progress)
                current.clear()
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    break

        proc.wait()
        stderr_thread.join(timeout=5)

        if cancel_event is not None and cancel_event.is_set():
            return EncodeResult(cancelled=True, returncode=proc.returncode or -1)

        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-self._stderr_tail:])
            raise EncodeError(f"ffmpeg exited with {proc.returncode}:\n{tail}")

        return EncodeResult(cancelled=False, returncode=0)

    @staticmethod
    def _make_progress(fields: dict[str, str], duration_s: float) -> EncodeProgress:
        out_time_s = 0.0
        if "out_time_us" in fields:
            try:
                out_time_s = int(fields["out_time_us"]) / 1_000_000
            except ValueError:
                pass
        elif "out_time_ms" in fields:
            try:
                out_time_s = int(fields["out_time_ms"]) / 1_000_000
            except ValueError:
                pass

        progress = 0.0
        if duration_s > 0:
            progress = max(0.0, min(1.0, out_time_s / duration_s))

        speed = _parse_speed(fields.get("speed", ""))
        eta_s: float | None = None
        if speed and speed > 0 and duration_s > 0:
            eta_s = max(0.0, (duration_s - out_time_s) / speed)

        return EncodeProgress(progress=progress, out_time_s=out_time_s, speed=speed, eta_s=eta_s)
