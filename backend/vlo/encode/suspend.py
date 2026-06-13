"""Suspend/resume a running process (Windows) to pause/resume an ffmpeg encode.

ffmpeg has no native pause, but freezing its OS process stops the encode (and
frees the CPU) and resuming continues it exactly where it left off. On Windows
this uses the undocumented-but-stable ``NtSuspendProcess`` / ``NtResumeProcess``
(ntdll), which suspend/resume every thread of the target process atomically.

All calls are best-effort: any failure returns ``False`` and changes nothing.
A suspended encode only survives within the same app session (the frozen
process); restarting the app loses it and the job is requeued.
"""

from __future__ import annotations

import os

_PROCESS_SUSPEND_RESUME = 0x0800


def _call_ntdll(pid: int, fn_name: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            return False
        try:
            status = getattr(ntdll, fn_name)(handle)  # NTSTATUS; 0 == success
            return status == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def suspend_process(pid: int) -> bool:
    """Freeze every thread of ``pid``. Returns True on success."""
    return _call_ntdll(pid, "NtSuspendProcess")


def resume_process(pid: int) -> bool:
    """Resume every thread of ``pid``. Returns True on success."""
    return _call_ntdll(pid, "NtResumeProcess")
