"""Tie a child process's lifetime to ours (Windows Job Object).

On Windows a child process is NOT killed when its parent dies, so an ffmpeg
encode would survive a uvicorn ``--reload`` restart (or a crash) as an orphan
that keeps burning CPU with no one to finalise it. Assigning the child to a Job
Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes Windows kill it
automatically once our process exits and the job handle is released.

The returned handle must be kept alive for the child's lifetime. Everything is
best-effort: any failure returns ``None`` and leaves the process running exactly
as before (no regression).
"""

from __future__ import annotations

import os


def bind_process_lifetime(pid: int):
    """Kill ``pid`` automatically when this process dies. Returns a handle or None.

    Keep the returned value referenced until the child exits; dropping it (or
    this process dying) closes the job and terminates the child.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_QUOTA = 0x0100

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(h_job)
            return None

        kernel32.OpenProcess.restype = wintypes.HANDLE
        h_proc = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
        if not h_proc:
            kernel32.CloseHandle(h_job)
            return None
        ok = kernel32.AssignProcessToJobObject(h_job, h_proc)
        kernel32.CloseHandle(h_proc)
        if not ok:
            kernel32.CloseHandle(h_job)
            return None
        return _JobHandle(kernel32, h_job)
    except Exception:
        return None


class _JobHandle:
    """Owns a job handle; closing it kills the assigned process (kill-on-close)."""

    def __init__(self, kernel32, handle) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
