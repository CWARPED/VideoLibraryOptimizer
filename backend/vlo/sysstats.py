"""System stats: CPU %, memory, and best-effort CPU temperature.

CPU load and memory come from ``psutil`` (reliable, in-process). CPU temperature
on Windows is not exposed by psutil and the ACPI thermal zone is usually denied
or meaningless (especially on Ryzen), so temperature is read best-effort from a
**LibreHardwareMonitor / OpenHardwareMonitor** WMI namespace — available only
when one of those tools is running. When none is present, temperature is ``None``
and the UI simply shows it as unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time

import psutil

logger = logging.getLogger("vlo.sys")

_TEMP_TTL_OK = 6.0     # re-read cadence once a source is found
_TEMP_TTL_MISS = 60.0  # back off when no source is present (avoid futile spawns)
_temp_cache: dict = {"value": None, "source": None, "at": -1e9}

_PS_TEMP = r"""
$ErrorActionPreference='SilentlyContinue'
foreach ($ns in @('LibreHardwareMonitor','OpenHardwareMonitor')) {
  $s = Get-CimInstance -Namespace "root/$ns" -ClassName Sensor |
       Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -like '*CPU*' }
  if ($s) {
    $v = $s | Where-Object { $_.Name -match 'Package|Tctl|Tdie' } | Select-Object -First 1
    if (-not $v) { $v = $s | Sort-Object Value -Descending | Select-Object -First 1 }
    Write-Output ('{0}|{1}' -f [math]::Round($v.Value,1), $ns)
    break
  }
}
"""


def _read_temp_source() -> tuple[float | None, str | None]:
    """Query a HardwareMonitor WMI namespace for a CPU temperature (or None)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TEMP],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
        if lines:
            val, _, src = lines[-1].partition("|")
            return float(val), (src or "hwmonitor")
    except Exception as exc:  # noqa: BLE001 - monitoring must never crash the app
        logger.debug("temperature read failed: %s", exc)
    return None, None


def cpu_temperature() -> tuple[float | None, str | None]:
    """Cached best-effort CPU temperature in °C (backs off when unavailable)."""
    now = time.monotonic()
    ttl = _TEMP_TTL_OK if _temp_cache["value"] is not None else _TEMP_TTL_MISS
    if now - _temp_cache["at"] < ttl:
        return _temp_cache["value"], _temp_cache["source"]
    val, src = _read_temp_source()
    _temp_cache.update(value=val, source=src, at=now)
    return val, src


def prime() -> None:
    """Prime psutil's cpu_percent so the first real sample isn't 0.0."""
    try:
        psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        pass


def sample() -> dict:
    """One system snapshot. ``cpu_percent`` is the load since the previous call."""
    vm = psutil.virtual_memory()
    temp, temp_src = cpu_temperature()
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "cpu_count": psutil.cpu_count(logical=True),
        "mem_percent": round(vm.percent, 1),
        "mem_used_bytes": vm.used,
        "mem_total_bytes": vm.total,
        "temp_c": temp,
        "temp_source": temp_src,
    }


async def broadcast_loop(broadcaster, interval: float = 2.0) -> None:
    """Publish a ``{"type": "system", ...}`` snapshot every ``interval`` seconds."""
    prime()
    await asyncio.sleep(1.0)
    while True:
        try:
            data = await asyncio.to_thread(sample)
            broadcaster.publish({"type": "system", **data})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("system stats sampling failed")
        await asyncio.sleep(interval)
