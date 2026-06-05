"""Download ffmpeg/ffprobe into the portable bundle (run at first launch).

Thin CLI wrapper around :mod:`vlo.ffmpeg_dist`. Resolves the bundle's
``ffmpeg/bin`` directory and downloads the binaries there if missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This file lives at <dist>/tools/fetch_ffmpeg.py; backend/ is a sibling.
_DIST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DIST / "backend"))

from vlo.ffmpeg_dist import FfmpegFetchError, ensure_ffmpeg  # noqa: E402


def main() -> int:
    dest = _DIST / "ffmpeg" / "bin"
    try:
        path = ensure_ffmpeg(dest)
    except FfmpegFetchError as exc:
        print(f"[VLO] {exc}", file=sys.stderr)
        return 1
    print(f"[VLO] ffmpeg prêt : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
