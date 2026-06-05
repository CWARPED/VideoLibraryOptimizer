"""Job-object kill-on-close behaviour (Windows only)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from vlo.encode.kill_on_close import bind_process_lifetime

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects only")


def test_closing_job_handle_kills_child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        handle = bind_process_lifetime(proc.pid)
        assert handle is not None  # job object created and process assigned
        handle.close()  # releasing the job -> child is killed
        # Child should die promptly.
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None  # process terminated by the job object
    finally:
        if proc.poll() is None:
            proc.kill()
