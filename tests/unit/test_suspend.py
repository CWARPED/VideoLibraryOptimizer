"""Process suspend/resume behaviour (Windows only)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from vlo.encode.suspend import resume_process, suspend_process

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NtSuspend/ResumeProcess is Windows-only")

# The venv python.exe is a launcher stub that spawns the real interpreter as a
# child; suspend it and the child keeps running. Use the base interpreter (a real
# exe with no stub) so we suspend the process actually doing the work — exactly
# like ffmpeg.exe in production.
_EXE = getattr(sys, "_base_executable", None) or sys.executable


def _cpu_ticks(pid: int) -> int:
    """Total kernel+user CPU time of ``pid`` in 100ns ticks (frozen when suspended)."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_INFORMATION = 0x0400
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    assert h, "OpenProcess failed"

    class FT(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    creation, exit_, kernel, user = FT(), FT(), FT(), FT()
    try:
        k.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                          ctypes.byref(kernel), ctypes.byref(user))
    finally:
        k.CloseHandle(h)
    return ((kernel.high << 32) | kernel.low) + ((user.high << 32) | user.low)


def test_suspend_freezes_then_resume_continues():
    proc = subprocess.Popen([_EXE, "-c", "while True: pass"])  # CPU-bound
    try:
        time.sleep(0.2)
        assert suspend_process(proc.pid)
        t1 = _cpu_ticks(proc.pid)
        time.sleep(0.3)
        t2 = _cpu_ticks(proc.pid)
        # Suspended -> burns ~no CPU (0.1s tolerance in 100ns ticks).
        assert (t2 - t1) < 1_000_000

        assert resume_process(proc.pid)
        time.sleep(0.3)
        t3 = _cpu_ticks(proc.pid)
        assert (t3 - t2) > 1_000_000  # running again -> CPU advances
    finally:
        proc.kill()
